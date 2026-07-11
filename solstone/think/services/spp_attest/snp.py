# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""SEV-SNP CPU-leg appraisal for SPP attestation."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.x509.oid import ExtensionOID, NameOID

from solstone.think.services.spp_attest.binding import (
    check_envelope_nonce,
    composite_binding_hash,
)
from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.tlv import decode_gpu_envelope
from solstone.think.services.spp_attest.tpm_quote import TpmQuoteVerifier

HCL_SIG = b"HCLA"
HCL_REPORT_OFFSET = 32
HCL_REPORT_SIZE = 1184
HCL_RUNTIME_OFFSET = HCL_REPORT_OFFSET + HCL_REPORT_SIZE

SNP_OFF_VERSION = 0x000
SNP_OFF_GUEST_SVN = 0x004
SNP_OFF_POLICY = 0x008
SNP_OFF_VMPL = 0x030
SNP_OFF_SIG_ALGO = 0x034
SNP_OFF_CURRENT_TCB = 0x038
SNP_OFF_PLATFORM_INFO = 0x040
SNP_OFF_KEY_INFO = 0x048
SNP_OFF_REPORT_DATA = 0x050
SNP_OFF_MEASUREMENT = 0x090
SNP_OFF_HOST_DATA = 0x0C0
SNP_OFF_REPORTED_TCB = 0x180
SNP_OFF_CPUID_FAMILY = 0x188
SNP_OFF_CPUID_MODEL = 0x189
SNP_OFF_CPUID_STEP = 0x18A
SNP_OFF_CHIP_ID = 0x1A0
SNP_OFF_COMMITTED_TCB = 0x1E0
SNP_OFF_CURRENT_VERSION = 0x1E8
SNP_OFF_COMMITTED_VERSION = 0x1EC
SNP_OFF_LAUNCH_TCB = 0x1F0
SNP_OFF_SIGNATURE = 0x2A0
SNP_SIGNED_PREFIX_LEN = 0x2A0
SNP_POLICY_DEBUG_BIT = 19

DEFAULT_ROOTS_DIR = Path(__file__).parent / "roots" / "amd"


@dataclass(frozen=True, slots=True)
class TcbVersion:
    boot_loader: int | None
    tee: int | None
    snp: int | None
    microcode: int | None
    fmc: int | None = None

    @classmethod
    def from_raw(cls, raw: bytes, generation: str) -> TcbVersion:
        if len(raw) != 8:
            raise VerificationError(f"TCB field is {len(raw)} bytes, expected 8")
        if generation == "turin":
            return cls(
                fmc=raw[0],
                boot_loader=raw[1],
                tee=raw[2],
                snp=raw[3],
                microcode=raw[7],
            )
        return cls(
            boot_loader=raw[0],
            tee=raw[1],
            snp=raw[6],
            microcode=raw[7],
        )

    def as_dict(self) -> dict[str, int | None]:
        return {
            "boot_loader": self.boot_loader,
            "tee": self.tee,
            "snp": self.snp,
            "microcode": self.microcode,
            "fmc": self.fmc,
        }


@dataclass(frozen=True, slots=True)
class TcbFloor:
    boot_loader: int | None = None
    tee: int | None = None
    snp: int | None = None
    microcode: int | None = None
    fmc: int | None = None

    def check(self, observed: TcbVersion, label: str) -> None:
        observed_values = observed.as_dict()
        for field_name in ("boot_loader", "tee", "snp", "microcode", "fmc"):
            floor_value = getattr(self, field_name)
            if floor_value is None:
                continue
            observed_value = observed_values[field_name]
            if observed_value is None:
                raise VerificationError(f"{label} TCB has no {field_name} field")
            if observed_value < floor_value:
                raise VerificationError(
                    f"{label} TCB {field_name}={observed_value} "
                    f"is below policy floor {floor_value}"
                )


@dataclass(frozen=True, slots=True)
class Policy:
    allowed_report_versions: set[int] = field(default_factory=lambda: {3, 5})
    allowed_hcla_versions: set[int] = field(default_factory=lambda: {1, 2})
    allowed_vmpl: set[int] = field(default_factory=lambda: {0})
    require_debug_disabled: bool = True
    min_tcb: dict[str, TcbFloor] = field(default_factory=dict)
    pcr_mode: str = "record"
    pcr_pins: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class SnpReport:
    raw: bytes
    version: int
    guest_svn: int
    policy: int
    vmpl: int
    sig_algo: int
    platform_info: int
    key_info: int
    report_data: bytes
    measurement: bytes
    host_data: bytes
    chip_id: bytes
    cpuid_family: int | None
    cpuid_model: int | None
    cpuid_step: int | None
    generation: str
    current_tcb: TcbVersion
    reported_tcb: TcbVersion
    committed_tcb: TcbVersion
    launch_tcb: TcbVersion
    current_version: str
    committed_version: str

    @classmethod
    def parse(cls, raw: bytes) -> SnpReport:
        if len(raw) != HCL_REPORT_SIZE:
            raise VerificationError(f"AMD report is {len(raw)} bytes, expected 1184")
        version = _u32(raw, SNP_OFF_VERSION)
        family = raw[SNP_OFF_CPUID_FAMILY] if version >= 3 else None
        model = raw[SNP_OFF_CPUID_MODEL] if version >= 3 else None
        step = raw[SNP_OFF_CPUID_STEP] if version >= 3 else None
        generation = _generation_for_cpuid(family, model)
        return cls(
            raw=raw,
            version=version,
            guest_svn=_u32(raw, SNP_OFF_GUEST_SVN),
            policy=_u64(raw, SNP_OFF_POLICY),
            vmpl=_u32(raw, SNP_OFF_VMPL),
            sig_algo=_u32(raw, SNP_OFF_SIG_ALGO),
            platform_info=_u64(raw, SNP_OFF_PLATFORM_INFO),
            key_info=_u32(raw, SNP_OFF_KEY_INFO),
            report_data=raw[SNP_OFF_REPORT_DATA : SNP_OFF_REPORT_DATA + 64],
            measurement=raw[SNP_OFF_MEASUREMENT : SNP_OFF_MEASUREMENT + 48],
            host_data=raw[SNP_OFF_HOST_DATA : SNP_OFF_HOST_DATA + 32],
            chip_id=raw[SNP_OFF_CHIP_ID : SNP_OFF_CHIP_ID + 64],
            cpuid_family=family,
            cpuid_model=model,
            cpuid_step=step,
            generation=generation,
            current_tcb=TcbVersion.from_raw(
                raw[SNP_OFF_CURRENT_TCB : SNP_OFF_CURRENT_TCB + 8],
                generation,
            ),
            reported_tcb=TcbVersion.from_raw(
                raw[SNP_OFF_REPORTED_TCB : SNP_OFF_REPORTED_TCB + 8],
                generation,
            ),
            committed_tcb=TcbVersion.from_raw(
                raw[SNP_OFF_COMMITTED_TCB : SNP_OFF_COMMITTED_TCB + 8],
                generation,
            ),
            launch_tcb=TcbVersion.from_raw(
                raw[SNP_OFF_LAUNCH_TCB : SNP_OFF_LAUNCH_TCB + 8],
                generation,
            ),
            current_version=_version(
                raw[SNP_OFF_CURRENT_VERSION : SNP_OFF_CURRENT_VERSION + 3]
            ),
            committed_version=_version(
                raw[SNP_OFF_COMMITTED_VERSION : SNP_OFF_COMMITTED_VERSION + 3]
            ),
        )

    @property
    def debug_allowed(self) -> bool:
        return ((self.policy >> SNP_POLICY_DEBUG_BIT) & 1) == 1


@dataclass(frozen=True, slots=True)
class AppraisalStep:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class CpuAppraisal:
    steps: list[AppraisalStep]
    hcla_version: int
    report_version: int
    cpuid: dict[str, int | None]
    tcb: dict[str, dict[str, int | None]]
    pcr_sha256: str
    host_data: str
    measurement: str
    chip_id: str


@dataclass(frozen=True, slots=True)
class _HclaBlob:
    version: int
    request_type: int
    report: bytes
    runtime_json: bytes
    runtime: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _AmdRootSet:
    product: str
    ark: x509.Certificate
    ask: x509.Certificate


def appraise_cpu_leg(
    bundle_dir: Path,
    *,
    envelope_tlv: bytes,
    channel_binding: bytes,
    roots_dir: Path | None = None,
    policy: Policy | None = None,
    quote_verifier: TpmQuoteVerifier | None = None,
) -> CpuAppraisal:
    """Appraise the CPU side of an SPP composite attestation bundle."""

    policy = policy or Policy()
    quote_verifier = quote_verifier or TpmQuoteVerifier()
    roots_dir = roots_dir or DEFAULT_ROOTS_DIR
    paths = _bundle_paths(bundle_dir)
    _require(
        paths, "hcl", "certs", "ak_pub", "nonce", "quote_msg", "quote_sig", "quote_pcrs"
    )

    nonce = _read_nonce(paths["nonce"])
    envelope = decode_gpu_envelope(envelope_tlv)
    check_envelope_nonce(envelope, nonce)

    steps: list[AppraisalStep] = []
    hcla = _parse_hcla(paths["hcl"].read_bytes())
    if hcla.version not in policy.allowed_hcla_versions:
        raise VerificationError(f"HCLA version {hcla.version} is not allowed")
    steps.append(
        _ok("hcla", f"sig=HCLA version={hcla.version} request_type={hcla.request_type}")
    )

    if paths["report"].exists() and paths["report"].read_bytes() != hcla.report:
        steps.append(
            _ok("standalone-report", "report.bin differs; using HCLA-embedded report")
        )

    report = SnpReport.parse(hcla.report)
    cpuid = {
        "family": report.cpuid_family,
        "model": report.cpuid_model,
        "step": report.cpuid_step,
    }
    tcb = {
        "current": report.current_tcb.as_dict(),
        "reported": report.reported_tcb.as_dict(),
        "committed": report.committed_tcb.as_dict(),
        "launch": report.launch_tcb.as_dict(),
    }

    _check_runtime_binding(report, hcla.runtime_json)
    steps.append(_ok("runtime-binding", "report_data == SHA-256(runtime JSON)"))

    vcek = _verify_amd_chain_and_report(report, paths["certs"], roots_dir)
    steps.append(
        _ok("amd-chain", f"VCEK chains to pinned {name_cn(vcek.issuer)} roots")
    )
    steps.append(_ok("amd-report-signature", "VCEK signed report bytes 0..0x29f"))

    _check_policy(report, policy)
    steps.append(
        _ok(
            "snp-policy",
            f"version={report.version} vmpl={report.vmpl} "
            f"debug_allowed={report.debug_allowed}",
        )
    )

    _verify_ak_binding(hcla.runtime, paths["ak_pub"])
    steps.append(_ok("ak-binding", "bundle AK public key matches AMD-bound HCLAkPub"))

    binding = composite_binding_hash(
        nonce=nonce,
        channel_binding=channel_binding,
        envelope_tlv=envelope_tlv,
    )
    quote_verifier.verify(
        paths["ak_pub"],
        paths["quote_msg"],
        paths["quote_sig"],
        paths["quote_pcrs"],
        binding.hex(),
    )
    steps.append(
        _ok(
            "quote",
            "AK quote signature valid and extraData matches verifier nonce + guest key",
        )
    )

    pcr_sha256 = hashlib.sha256(paths["quote_pcrs"].read_bytes()).hexdigest()
    _check_pcr_policy(pcr_sha256, policy)
    if policy.pcr_mode == "record":
        steps.append(_ok("pcr-policy", f"record-then-pin v1 fingerprint={pcr_sha256}"))
    else:
        steps.append(_ok("pcr-policy", f"pinned PCR fingerprint matched {pcr_sha256}"))

    return CpuAppraisal(
        steps=steps,
        hcla_version=hcla.version,
        report_version=report.version,
        cpuid=cpuid,
        tcb=tcb,
        pcr_sha256=pcr_sha256,
        host_data=report.host_data.hex(),
        measurement=report.measurement.hex(),
        chip_id=report.chip_id.hex(),
    )


def name_cn(name: x509.Name) -> str:
    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else name.rfc4514_string()


def _ok(name: str, detail: str) -> AppraisalStep:
    return AppraisalStep(name=name, status="ok", detail=detail)


def _parse_hcla(blob: bytes) -> _HclaBlob:
    if len(blob) < HCL_RUNTIME_OFFSET:
        raise VerificationError(
            f"HCLA blob is {len(blob)} bytes; expected at least {HCL_RUNTIME_OFFSET}"
        )
    sig = blob[:4]
    version = _u32(blob, 4)
    request_type = _u32(blob, 12)
    if sig != HCL_SIG:
        raise VerificationError(f"HCLA signature mismatch: {sig!r}")
    if request_type != 2:
        raise VerificationError(
            f"HCLA request_type={request_type}, expected AMD-SNP request_type 2"
        )
    report = blob[HCL_REPORT_OFFSET : HCL_REPORT_OFFSET + HCL_REPORT_SIZE]
    runtime_json = _extract_runtime_json(blob)
    try:
        runtime = json.loads(runtime_json)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"HCL runtime JSON did not parse: {exc}") from exc
    if not isinstance(runtime, dict):
        raise VerificationError("HCL runtime JSON is not an object")
    return _HclaBlob(
        version=version,
        request_type=request_type,
        report=report,
        runtime_json=runtime_json,
        runtime=runtime,
    )


def _extract_runtime_json(blob: bytes) -> bytes:
    start = blob.find(b'{"', HCL_RUNTIME_OFFSET)
    if start < 0:
        raise VerificationError(
            f"no JSON object found at/after HCLA offset {HCL_RUNTIME_OFFSET}"
        )
    end = blob.find(b"\x00", start)
    if end < 0:
        end = len(blob)
    return blob[start:end]


def _check_runtime_binding(report: SnpReport, runtime_json: bytes) -> None:
    digest = hashlib.sha256(runtime_json).digest()
    if report.report_data[:32] != digest:
        raise VerificationError(
            "runtime-data binding failed: "
            f"SHA-256(runtime)={digest.hex()} "
            f"report_data={report.report_data[:32].hex()}"
        )
    if report.report_data[32:] != b"\x00" * 32:
        raise VerificationError(
            "report_data[32..64] is nonzero; expected SHA-256 runtime binding"
        )


def _verify_amd_chain_and_report(
    report: SnpReport,
    certs_dir: Path,
    roots_dir: Path,
) -> x509.Certificate:
    certs = _load_certs_from_dir(certs_dir)
    vcek = _select_vcek(certs)
    root = _select_root_set(vcek, _load_root_sets(roots_dir))
    _verify_cert_signature(root.ask, root.ark)
    _verify_cert_signature(root.ark, root.ark)
    _verify_cert_signature(vcek, root.ask)
    for cert in [root.ark, root.ask, vcek]:
        _check_cert_time(cert)
    _reject_mismatched_bundle_cas(certs, root)
    _verify_report_signature(report.raw, vcek)
    return vcek


def _load_certs_from_dir(certs_dir: Path) -> list[x509.Certificate]:
    if not certs_dir.is_dir():
        raise VerificationError(f"missing certs directory: {certs_dir}")
    certs: list[x509.Certificate] = []
    for path in sorted(certs_dir.glob("*.pem")):
        try:
            certs.append(x509.load_pem_x509_certificate(path.read_bytes()))
        except ValueError as exc:
            raise VerificationError(f"certificate did not parse: {path}") from exc
    if not certs:
        raise VerificationError(f"no PEM certificates in {certs_dir}")
    return certs


def _load_root_sets(roots_dir: Path) -> list[_AmdRootSet]:
    root_sets: list[_AmdRootSet] = []
    for product_dir in sorted(roots_dir.glob("*")):
        if not product_dir.is_dir():
            continue
        ark_path = product_dir / "ark.pem"
        ask_path = product_dir / "ask.pem"
        if not ark_path.exists() or not ask_path.exists():
            continue
        try:
            ark = x509.load_pem_x509_certificate(ark_path.read_bytes())
            ask = x509.load_pem_x509_certificate(ask_path.read_bytes())
        except ValueError as exc:
            raise VerificationError(
                f"AMD root set did not parse: {product_dir}"
            ) from exc
        if not _is_ca(ark):
            raise VerificationError(f"AMD root set {product_dir.name} ARK is not a CA")
        if not _is_ca(ask):
            raise VerificationError(f"AMD root set {product_dir.name} ASK is not a CA")
        root_sets.append(
            _AmdRootSet(
                product=product_dir.name,
                ark=ark,
                ask=ask,
            )
        )
    if not root_sets:
        raise VerificationError(f"no AMD root sets under {roots_dir}")
    return root_sets


def _select_vcek(certs: list[x509.Certificate]) -> x509.Certificate:
    candidates = [
        cert
        for cert in certs
        if not _is_ca(cert) and isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    ]
    if len(candidates) != 1:
        raise VerificationError(
            f"expected exactly one VCEK/VLEK cert, found {len(candidates)}"
        )
    return candidates[0]


def _select_root_set(
    vcek: x509.Certificate, root_sets: list[_AmdRootSet]
) -> _AmdRootSet:
    issuer = name_cn(vcek.issuer)
    for root in root_sets:
        if name_cn(root.ask.subject) == issuer:
            return root
    products = ", ".join(
        f"{root.product}:{name_cn(root.ask.subject)}" for root in root_sets
    )
    raise VerificationError(
        f"no pinned AMD ASK for VCEK issuer {issuer}; available {products}"
    )


def _reject_mismatched_bundle_cas(
    certs: list[x509.Certificate],
    root: _AmdRootSet,
) -> None:
    pinned = {
        name_cn(root.ark.subject): root.ark.fingerprint(hashes.SHA256()),
        name_cn(root.ask.subject): root.ask.fingerprint(hashes.SHA256()),
    }
    for cert in certs:
        subject = name_cn(cert.subject)
        if subject in pinned and cert.fingerprint(hashes.SHA256()) != pinned[subject]:
            raise VerificationError(
                f"bundle CA {subject} does not match pinned root material"
            )


def _verify_cert_signature(cert: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            params = cert.signature_algorithm_parameters
            if params is None:
                params = asym_padding.PKCS1v15()
            public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                params,
                cert.signature_hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),
            )
        else:
            raise VerificationError(
                f"unsupported issuer key type: {type(public_key).__name__}"
            )
    except InvalidSignature as exc:
        raise VerificationError(
            f"certificate signature invalid: {name_cn(cert.subject)} <- {name_cn(issuer.subject)}"
        ) from exc


def _verify_report_signature(report: bytes, vcek: x509.Certificate) -> None:
    if len(report) != HCL_REPORT_SIZE:
        raise VerificationError(f"report length {len(report)} != {HCL_REPORT_SIZE}")
    raw_sig = report[SNP_OFF_SIGNATURE : SNP_OFF_SIGNATURE + 512]
    if raw_sig[144:] != b"\x00" * (512 - 144):
        raise VerificationError("AMD report signature reserved bytes are nonzero")
    r = int.from_bytes(raw_sig[:72], "little")
    s = int.from_bytes(raw_sig[72:144], "little")
    der_sig = utils.encode_dss_signature(r, s)
    public_key = vcek.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise VerificationError("VCEK public key is not an EC key")
    try:
        public_key.verify(
            der_sig, report[:SNP_SIGNED_PREFIX_LEN], ec.ECDSA(hashes.SHA384())
        )
    except InvalidSignature as exc:
        raise VerificationError("VCEK did not sign the AMD report") from exc


def _check_policy(report: SnpReport, policy: Policy) -> None:
    if report.version not in policy.allowed_report_versions:
        raise VerificationError(f"SNP report version {report.version} not allowed")
    if policy.allowed_vmpl and report.vmpl not in policy.allowed_vmpl:
        raise VerificationError(f"VMPL {report.vmpl} not allowed")
    if policy.require_debug_disabled and report.debug_allowed:
        raise VerificationError("SNP guest policy allows DEBUG")
    tcb_fields = {
        "current": report.current_tcb,
        "reported": report.reported_tcb,
        "committed": report.committed_tcb,
        "launch": report.launch_tcb,
    }
    for label, floor in policy.min_tcb.items():
        if label not in tcb_fields:
            raise VerificationError(f"unknown TCB policy label: {label}")
        floor.check(tcb_fields[label], label)


def _verify_ak_binding(runtime: dict[str, Any], ak_pub_path: Path) -> None:
    keys = runtime.get("keys", [])
    if not isinstance(keys, list):
        raise VerificationError("runtime claims field 'keys' is not a list")
    jwk = next(
        (key for key in keys if isinstance(key, dict) and key.get("kid") == "HCLAkPub"),
        None,
    )
    if jwk is None:
        raise VerificationError("HCLAkPub not found in HCL runtime claims")
    if "n" not in jwk or "e" not in jwk:
        raise VerificationError("HCLAkPub JWK is missing RSA modulus or exponent")
    runtime_n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    runtime_e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    try:
        ak_pub = serialization.load_pem_public_key(ak_pub_path.read_bytes())
    except ValueError as exc:
        raise VerificationError("bundle AK public key did not parse") from exc
    if not isinstance(ak_pub, rsa.RSAPublicKey):
        raise VerificationError("bundle AK public key is not RSA")
    ak_numbers = ak_pub.public_numbers()
    if ak_numbers.n != runtime_n or ak_numbers.e != runtime_e:
        raise VerificationError(
            "bundle AK public key does not match AMD-bound HCLAkPub"
        )


def _check_pcr_policy(pcr_sha256: str, policy: Policy) -> None:
    if policy.pcr_mode == "record":
        return
    if policy.pcr_mode != "pin":
        raise VerificationError(f"unknown PCR policy mode {policy.pcr_mode!r}")
    pins = {pin.lower() for pin in policy.pcr_pins}
    if pcr_sha256.lower() not in pins:
        raise VerificationError(f"PCR fingerprint {pcr_sha256} not in pinned policy")


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_oid(
            ExtensionOID.BASIC_CONSTRAINTS
        ).value.ca
    except x509.ExtensionNotFound:
        return False


def _check_cert_time(cert: x509.Certificate) -> None:
    now = dt.datetime.now(dt.UTC)
    if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
        raise VerificationError(
            f"certificate outside validity window: {name_cn(cert.subject)}"
        )


def _bundle_paths(bundle: Path) -> dict[str, Path]:
    return {
        "hcl": bundle / "hcl_report.bin",
        "report": bundle / "report.bin",
        "certs": bundle / "certs",
        "ak_pub": bundle / "akpub.pem",
        "nonce": bundle / "nonce.hex",
        "quote_msg": bundle / "quote.msg",
        "quote_sig": bundle / "quote.sig",
        "quote_pcrs": bundle / "quote.pcrs",
    }


def _require(paths: dict[str, Path], *names: str) -> None:
    missing = [str(paths[name]) for name in names if not paths[name].exists()]
    if missing:
        raise VerificationError("missing bundle files: " + ", ".join(missing))


def _read_nonce(path: Path) -> bytes:
    nonce_hex = "".join(path.read_text(encoding="utf-8").split())
    if len(nonce_hex) != 64:
        raise VerificationError(f"nonce is {len(nonce_hex)} hex chars, expected 64")
    try:
        return bytes.fromhex(nonce_hex)
    except ValueError as exc:
        raise VerificationError("nonce is not valid hex") from exc


def _u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise VerificationError(f"u32 read at offset {offset} overruns buffer")
    return int.from_bytes(data[offset : offset + 4], "little")


def _u64(data: bytes, offset: int) -> int:
    if offset + 8 > len(data):
        raise VerificationError(f"u64 read at offset {offset} overruns buffer")
    return int.from_bytes(data[offset : offset + 8], "little")


def _version(raw: bytes) -> str:
    if len(raw) != 3:
        return "unknown"
    return f"{raw[2]}.{raw[1]}.{raw[0]}"


def _generation_for_cpuid(family: int | None, model: int | None) -> str:
    if (
        family == 0x1A
        and model is not None
        and (0x90 <= model <= 0xAF or 0xC0 <= model <= 0xCF)
    ):
        return "turin"
    return "pre_turin"


def _b64url_decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise VerificationError("HCLAkPub JWK field is not a string")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            (value + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise VerificationError("HCLAkPub JWK field is not valid base64url") from exc
