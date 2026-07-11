# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure-Python TPM2 quote verification for SPP attestation."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from solstone.think.services.spp_attest.errors import VerificationError

TPM_GENERATED_VALUE = 0xFF544347
TPM_ST_ATTEST_QUOTE = 0x8018
TPM_ALG_SHA256 = 0x000B
TPM_ALG_RSASSA = 0x0014
TPM_ALG_RSAPSS = 0x0016
SHA256_DIGEST_SIZE = 32
PCR_SELECTION_SLOT_COUNT = 8
PCR_DIGEST_SLOT_COUNT = 8
PCR_DIGEST_BUFFER_SIZE = 64


@dataclass(frozen=True, slots=True)
class _PcrSelection:
    hash_alg: int
    sizeof_select: int
    pcr_select: bytes

    def selected_pcrs(self) -> tuple[int, ...]:
        selected: list[int] = []
        for byte_index, value in enumerate(self.pcr_select):
            for bit in range(8):
                if value & (1 << bit):
                    selected.append(byte_index * 8 + bit)
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class _QuoteInfo:
    extra_data: bytes
    selections: tuple[_PcrSelection, ...]
    pcr_digest: bytes


@dataclass(frozen=True, slots=True)
class _SignatureInfo:
    sig_alg: int
    hash_alg: int
    signature: bytes


class _Reader:
    def __init__(self, data: bytes, label: str) -> None:
        self._data = data
        self._label = label
        self.offset = 0

    def read(self, size: int, field: str) -> bytes:
        if size < 0:
            raise VerificationError(f"{self._label} field {field} has negative size")
        end = self.offset + size
        if end > len(self._data):
            raise VerificationError(f"{self._label} field {field} overruns buffer")
        value = self._data[self.offset : end]
        self.offset = end
        return value

    def u8(self, field: str) -> int:
        return self.read(1, field)[0]

    def u16be(self, field: str) -> int:
        return int.from_bytes(self.read(2, field), "big")

    def u32be(self, field: str) -> int:
        return int.from_bytes(self.read(4, field), "big")

    def u64be(self, field: str) -> int:
        return int.from_bytes(self.read(8, field), "big")

    def u16le(self, field: str) -> int:
        return int.from_bytes(self.read(2, field), "little")

    def u32le(self, field: str) -> int:
        return int.from_bytes(self.read(4, field), "little")

    def require_consumed(self) -> None:
        if self.offset != len(self._data):
            raise VerificationError(f"{self._label} has trailing bytes")


def verify_quote(
    *,
    ak_pub_pem: bytes,
    quote_msg: bytes,
    quote_sig: bytes,
    quote_pcrs: bytes,
    expected_binding: bytes,
) -> None:
    """Verify a TPM2 quote, its extraData binding, signature, and PCR digest."""

    public_key = _load_ak_public_key(ak_pub_pem)
    quote = _parse_quote_msg(quote_msg, expected_binding)
    _check_pcrs(quote_pcrs, quote)
    signature = _parse_quote_sig(quote_sig, public_key.key_size // 8)
    _verify_signature(public_key, quote_msg, signature)


class TpmQuoteVerifier:
    def verify(
        self,
        ak_pub: Path,
        quote_msg: Path,
        quote_sig: Path,
        quote_pcrs: Path,
        binding_hex: str,
    ) -> None:
        try:
            binding = bytes.fromhex(binding_hex)
        except ValueError as exc:
            raise VerificationError("TPM quote binding_hex is not valid hex") from exc
        verify_quote(
            ak_pub_pem=ak_pub.read_bytes(),
            quote_msg=quote_msg.read_bytes(),
            quote_sig=quote_sig.read_bytes(),
            quote_pcrs=quote_pcrs.read_bytes(),
            expected_binding=binding,
        )


def _load_ak_public_key(ak_pub_pem: bytes) -> rsa.RSAPublicKey:
    try:
        public_key = serialization.load_pem_public_key(ak_pub_pem)
    except ValueError as exc:
        raise VerificationError("AK public key PEM did not parse") from exc
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise VerificationError("AK public key is not RSA")
    return public_key


def _parse_quote_msg(quote_msg: bytes, expected_binding: bytes) -> _QuoteInfo:
    if len(expected_binding) != SHA256_DIGEST_SIZE:
        raise VerificationError(
            f"expected TPM quote binding is {len(expected_binding)} bytes, expected 32"
        )

    reader = _Reader(quote_msg, "TPMS_ATTEST")
    magic = reader.u32be("magic")
    if magic != TPM_GENERATED_VALUE:
        raise VerificationError(f"TPMS_ATTEST magic 0x{magic:08x} != 0xff544347")
    attest_type = reader.u16be("type")
    if attest_type != TPM_ST_ATTEST_QUOTE:
        raise VerificationError(f"TPMS_ATTEST type 0x{attest_type:04x} is not quote")

    qualified_signer_size = reader.u16be("qualifiedSigner.size")
    reader.read(qualified_signer_size, "qualifiedSigner.name")

    extra_data_size = reader.u16be("extraData.size")
    extra_data = reader.read(extra_data_size, "extraData.buffer")
    if not hmac.compare_digest(extra_data, expected_binding):
        raise VerificationError(
            "TPM quote extraData mismatch: "
            f"quote={extra_data.hex()} expected={expected_binding.hex()}"
        )

    reader.u64be("clockInfo.clock")
    reader.u32be("clockInfo.resetCount")
    reader.u32be("clockInfo.restartCount")
    reader.u8("clockInfo.safe")
    reader.u64be("firmwareVersion")

    selection_count = reader.u32be("attested.quote.pcrSelect.count")
    if selection_count != 1:
        raise VerificationError(
            f"TPM quote selection count {selection_count} unsupported; expected 1"
        )

    selections: list[_PcrSelection] = []
    for index in range(selection_count):
        hash_alg = reader.u16be(f"attested.quote.pcrSelect[{index}].hash")
        if hash_alg != TPM_ALG_SHA256:
            raise VerificationError(
                f"TPM quote PCR hashAlg 0x{hash_alg:04x} unsupported"
            )
        sizeof_select = reader.u8(f"attested.quote.pcrSelect[{index}].sizeofSelect")
        if sizeof_select < 1:
            raise VerificationError(
                f"TPM quote PCR sizeofSelect {sizeof_select} is empty"
            )
        if sizeof_select > PCR_SELECTION_SLOT_COUNT:
            raise VerificationError(
                f"TPM quote PCR sizeofSelect {sizeof_select} exceeds 8"
            )
        pcr_select = reader.read(
            sizeof_select,
            f"attested.quote.pcrSelect[{index}].pcrSelect",
        )
        selection = _PcrSelection(
            hash_alg=hash_alg,
            sizeof_select=sizeof_select,
            pcr_select=pcr_select,
        )
        if not selection.selected_pcrs():
            raise VerificationError("TPM quote PCR selection selects no PCRs")
        selections.append(selection)

    pcr_digest_size = reader.u16be("attested.quote.pcrDigest.size")
    if pcr_digest_size != SHA256_DIGEST_SIZE:
        raise VerificationError(
            f"TPM quote pcrDigest is {pcr_digest_size} bytes, expected 32"
        )
    pcr_digest = reader.read(pcr_digest_size, "attested.quote.pcrDigest.buffer")
    reader.require_consumed()
    return _QuoteInfo(
        extra_data=extra_data,
        selections=tuple(selections),
        pcr_digest=pcr_digest,
    )


def _parse_quote_sig(quote_sig: bytes, key_size_bytes: int) -> _SignatureInfo:
    reader = _Reader(quote_sig, "TPMT_SIGNATURE")
    sig_alg = reader.u16be("sigAlg")
    if sig_alg not in {TPM_ALG_RSASSA, TPM_ALG_RSAPSS}:
        raise VerificationError(f"TPM signature alg 0x{sig_alg:04x} unsupported")
    hash_alg = reader.u16be("hashAlg")
    if hash_alg != TPM_ALG_SHA256:
        raise VerificationError(f"TPM signature hashAlg 0x{hash_alg:04x} unsupported")
    signature_size = reader.u16be("signature.size")
    if signature_size != key_size_bytes:
        raise VerificationError(
            f"TPM signature is {signature_size} bytes, expected RSA key size {key_size_bytes}"
        )
    signature = reader.read(signature_size, "signature.buffer")
    reader.require_consumed()
    return _SignatureInfo(sig_alg=sig_alg, hash_alg=hash_alg, signature=signature)


def _verify_signature(
    public_key: rsa.RSAPublicKey,
    quote_msg: bytes,
    signature: _SignatureInfo,
) -> None:
    try:
        if signature.sig_alg == TPM_ALG_RSASSA:
            public_key.verify(
                signature.signature,
                quote_msg,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        else:
            public_key.verify(
                signature.signature,
                quote_msg,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=SHA256_DIGEST_SIZE,
                ),
                hashes.SHA256(),
            )
    except InvalidSignature as exc:
        raise VerificationError("TPM quote signature invalid") from exc


def _check_pcrs(quote_pcrs: bytes, quote: _QuoteInfo) -> None:
    pcrs_selections, digest_buffers = _parse_pcrs(quote_pcrs)
    if pcrs_selections != quote.selections:
        raise VerificationError("TPM PCR selection file does not match quote selection")

    selected_count = sum(
        len(selection.selected_pcrs()) for selection in pcrs_selections
    )
    if selected_count != len(digest_buffers):
        raise VerificationError(
            f"TPM PCR digest count {len(digest_buffers)} does not match "
            f"selected PCR count {selected_count}"
        )

    pcr_digest = hashlib.sha256(b"".join(digest_buffers)).digest()
    if pcr_digest != quote.pcr_digest:
        raise VerificationError(
            "TPM PCR digest mismatch: "
            f"computed={pcr_digest.hex()} quote={quote.pcr_digest.hex()}"
        )


def _parse_pcrs(
    quote_pcrs: bytes,
) -> tuple[tuple[_PcrSelection, ...], tuple[bytes, ...]]:
    reader = _Reader(quote_pcrs, "quote.pcrs")
    selection_count = reader.u32le("selection_count")
    if selection_count < 1:
        raise VerificationError("quote.pcrs selection_count is empty")
    if selection_count > PCR_SELECTION_SLOT_COUNT:
        raise VerificationError(
            f"quote.pcrs selection_count {selection_count} exceeds 8"
        )

    selections: list[_PcrSelection] = []
    for index in range(PCR_SELECTION_SLOT_COUNT):
        hash_alg = reader.u16le(f"selection[{index}].hashAlg")
        sizeof_select = reader.u8(f"selection[{index}].sizeofSelect")
        pcr_select_slot = reader.read(8, f"selection[{index}].pcrSelect")
        pad = reader.read(5, f"selection[{index}].pad")
        if pad != b"\x00" * 5:
            raise VerificationError(
                f"quote.pcrs selection[{index}] pad bytes are nonzero"
            )
        if index >= selection_count:
            if hash_alg != 0 or sizeof_select != 0 or any(pcr_select_slot):
                raise VerificationError(
                    f"quote.pcrs inactive selection[{index}] is nonzero"
                )
            continue
        if hash_alg != TPM_ALG_SHA256:
            raise VerificationError(f"quote.pcrs hashAlg 0x{hash_alg:04x} unsupported")
        if sizeof_select < 1:
            raise VerificationError(
                f"quote.pcrs selection[{index}] sizeofSelect is empty"
            )
        if sizeof_select > PCR_SELECTION_SLOT_COUNT:
            raise VerificationError(
                f"quote.pcrs selection[{index}] sizeofSelect {sizeof_select} exceeds 8"
            )
        if any(pcr_select_slot[sizeof_select:]):
            raise VerificationError(
                f"quote.pcrs selection[{index}] has nonzero bytes after sizeofSelect"
            )
        selection = _PcrSelection(
            hash_alg=hash_alg,
            sizeof_select=sizeof_select,
            pcr_select=pcr_select_slot[:sizeof_select],
        )
        if not selection.selected_pcrs():
            raise VerificationError(f"quote.pcrs selection[{index}] selects no PCRs")
        selections.append(selection)

    digest_list_count = reader.u32le("digest_list_count")
    digest_buffers: list[bytes] = []
    for list_index in range(digest_list_count):
        count = reader.u32le(f"digest_list[{list_index}].count")
        if count > PCR_DIGEST_SLOT_COUNT:
            raise VerificationError(
                f"quote.pcrs digest_list[{list_index}] count {count} exceeds 8"
            )
        for digest_index in range(PCR_DIGEST_SLOT_COUNT):
            size = reader.u16le(
                f"digest_list[{list_index}].digest[{digest_index}].size"
            )
            buffer = reader.read(
                PCR_DIGEST_BUFFER_SIZE,
                f"digest_list[{list_index}].digest[{digest_index}].buffer",
            )
            if size > PCR_DIGEST_BUFFER_SIZE:
                raise VerificationError(
                    f"quote.pcrs digest_list[{list_index}].digest[{digest_index}] "
                    f"size {size} exceeds 64"
                )
            if digest_index >= count:
                if size != 0 or any(buffer):
                    raise VerificationError(
                        f"quote.pcrs digest_list[{list_index}].digest[{digest_index}] "
                        "inactive slot is nonzero"
                    )
                continue
            if size != SHA256_DIGEST_SIZE:
                raise VerificationError(
                    f"quote.pcrs digest_list[{list_index}].digest[{digest_index}] "
                    f"size {size} != 32"
                )
            digest_buffers.append(buffer[:size])

    reader.require_consumed()
    return tuple(selections), tuple(digest_buffers)
