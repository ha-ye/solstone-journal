# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Vulkan GPU discovery for the bundled local provider."""

from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

VK_TYPE_OTHER = 0
VK_TYPE_INTEGRATED = 1
VK_TYPE_DISCRETE = 2
VK_TYPE_VIRTUAL = 3
VK_TYPE_CPU = 4

_SOFTWARE_NAME_SUBSTRINGS = ("llvmpipe", "lavapipe", "swiftshader")
_PROBE_TIMEOUT_S = 10.0
_VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO = 1
_VK_PHYSICAL_DEVICE_MEMORY_PROPERTY_DEVICE_LOCAL_BIT = 0x00000001
_VK_PHYSICAL_DEVICE_NAME_SIZE = 256
_VK_MAX_MEMORY_TYPES = 32
_VK_MAX_MEMORY_HEAPS = 16

_DETECT_CACHE: list["VulkanDevice"] | None = None


@dataclass(frozen=True)
class VulkanDevice:
    index: int
    name: str
    device_type: int
    vram_mib: int


class _VkInstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
        ("pApplicationInfo", ctypes.c_void_p),
        ("enabledLayerCount", ctypes.c_uint32),
        ("ppEnabledLayerNames", ctypes.c_void_p),
        ("enabledExtensionCount", ctypes.c_uint32),
        ("ppEnabledExtensionNames", ctypes.c_void_p),
    ]


class _VkPhysicalDeviceProperties(ctypes.Structure):
    _fields_ = [
        ("apiVersion", ctypes.c_uint32),
        ("driverVersion", ctypes.c_uint32),
        ("vendorID", ctypes.c_uint32),
        ("deviceID", ctypes.c_uint32),
        ("deviceType", ctypes.c_uint32),
        ("deviceName", ctypes.c_char * _VK_PHYSICAL_DEVICE_NAME_SIZE),
        ("_tail", ctypes.c_ubyte * 8192),
    ]


class _VkMemoryType(ctypes.Structure):
    _fields_ = [
        ("propertyFlags", ctypes.c_uint32),
        ("heapIndex", ctypes.c_uint32),
    ]


class _VkMemoryHeap(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
    ]


class _VkPhysicalDeviceMemoryProperties(ctypes.Structure):
    _fields_ = [
        ("memoryTypeCount", ctypes.c_uint32),
        ("memoryTypes", _VkMemoryType * _VK_MAX_MEMORY_TYPES),
        ("memoryHeapCount", ctypes.c_uint32),
        ("memoryHeaps", _VkMemoryHeap * _VK_MAX_MEMORY_HEAPS),
    ]


def _enumerate_in_process() -> list[VulkanDevice]:
    """Enumerate Vulkan devices in the current process.

    This is invoked by the module subprocess entry point; supervisor-facing code
    calls ``detect_gpus()`` so an unhealthy ICD cannot hang the parent process.
    """
    try:
        vulkan = ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return []

    instance = ctypes.c_void_p()
    devices: list[VulkanDevice] = []
    try:
        vulkan.vkCreateInstance.argtypes = [
            ctypes.POINTER(_VkInstanceCreateInfo),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        vulkan.vkCreateInstance.restype = ctypes.c_int32
        vulkan.vkDestroyInstance.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        vulkan.vkDestroyInstance.restype = None
        vulkan.vkEnumeratePhysicalDevices.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        vulkan.vkEnumeratePhysicalDevices.restype = ctypes.c_int32
        vulkan.vkGetPhysicalDeviceProperties.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_VkPhysicalDeviceProperties),
        ]
        vulkan.vkGetPhysicalDeviceProperties.restype = None
        vulkan.vkGetPhysicalDeviceMemoryProperties.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_VkPhysicalDeviceMemoryProperties),
        ]
        vulkan.vkGetPhysicalDeviceMemoryProperties.restype = None

        create_info = _VkInstanceCreateInfo(
            sType=_VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
            pNext=None,
            flags=0,
            pApplicationInfo=None,
            enabledLayerCount=0,
            ppEnabledLayerNames=None,
            enabledExtensionCount=0,
            ppEnabledExtensionNames=None,
        )
        if (
            vulkan.vkCreateInstance(
                ctypes.byref(create_info), None, ctypes.byref(instance)
            )
            != 0
        ):
            return []

        count = ctypes.c_uint32(0)
        if vulkan.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None) != 0:
            return []
        if count.value == 0:
            return []

        device_array_type = ctypes.c_void_p * count.value
        raw_devices = device_array_type()
        if (
            vulkan.vkEnumeratePhysicalDevices(
                instance,
                ctypes.byref(count),
                ctypes.cast(raw_devices, ctypes.POINTER(ctypes.c_void_p)),
            )
            != 0
        ):
            return []

        for index, raw_device in enumerate(raw_devices[: count.value]):
            props = _VkPhysicalDeviceProperties()
            vulkan.vkGetPhysicalDeviceProperties(raw_device, ctypes.byref(props))
            name = (
                bytes(props.deviceName)
                .split(b"\0", 1)[0]
                .decode("utf-8", errors="replace")
            )

            mem_props = _VkPhysicalDeviceMemoryProperties()
            vulkan.vkGetPhysicalDeviceMemoryProperties(
                raw_device, ctypes.byref(mem_props)
            )
            vram_bytes = 0
            for heap in mem_props.memoryHeaps[: mem_props.memoryHeapCount]:
                if heap.flags & _VK_PHYSICAL_DEVICE_MEMORY_PROPERTY_DEVICE_LOCAL_BIT:
                    vram_bytes += int(heap.size)

            devices.append(
                VulkanDevice(
                    index=index,
                    name=name,
                    device_type=int(props.deviceType),
                    vram_mib=vram_bytes // (1024 * 1024),
                )
            )
    except Exception:
        return []
    finally:
        if instance.value:
            try:
                vulkan.vkDestroyInstance(instance, None)
            except Exception:
                pass
    return devices


def _devices_from_json(text: str) -> list[VulkanDevice]:
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError("Vulkan probe output was not a list")
    devices: list[VulkanDevice] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Vulkan probe item was not an object")
        devices.append(
            VulkanDevice(
                index=int(item["index"]),
                name=str(item["name"]),
                device_type=int(item["device_type"]),
                vram_mib=int(item["vram_mib"]),
            )
        )
    return devices


def _enumerate_gpus() -> list[VulkanDevice]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "solstone.think.providers.local_vulkan"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Vulkan GPU probe timed out after %.0fs", _PROBE_TIMEOUT_S)
        return []
    except OSError as exc:
        logger.warning("Vulkan GPU probe could not start: %s", exc)
        return []

    if completed.returncode != 0:
        logger.warning("Vulkan GPU probe exited with status %s", completed.returncode)
        return []

    try:
        devices = _devices_from_json(completed.stdout)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Vulkan GPU probe returned invalid JSON: %s", exc)
        return []

    if not devices:
        logger.warning("Vulkan GPU probe returned no devices")
        return []
    return devices


def detect_gpus() -> list[VulkanDevice]:
    global _DETECT_CACHE
    if _DETECT_CACHE is None:
        _DETECT_CACHE = _enumerate_gpus()
    return list(_DETECT_CACHE)


def reset_detect_cache() -> None:
    global _DETECT_CACHE
    _DETECT_CACHE = None


def is_hardware_device(dev: VulkanDevice) -> bool:
    name = dev.name.lower()
    if any(substring in name for substring in _SOFTWARE_NAME_SUBSTRINGS):
        return False
    return dev.device_type in {VK_TYPE_INTEGRATED, VK_TYPE_DISCRETE}


def select_device(
    devices: list[VulkanDevice], override_index: int | None = None
) -> VulkanDevice | None:
    if override_index is not None:
        for dev in devices:
            if dev.index == override_index and is_hardware_device(dev):
                return dev
        return None

    hardware = [dev for dev in devices if is_hardware_device(dev)]
    for device_type in (VK_TYPE_DISCRETE, VK_TYPE_INTEGRATED):
        matches = sorted(
            (dev for dev in hardware if dev.device_type == device_type),
            key=lambda dev: dev.index,
        )
        if matches:
            return matches[0]
    return None


def classify(dev: VulkanDevice) -> str:
    if any(substring in dev.name.lower() for substring in _SOFTWARE_NAME_SUBSTRINGS):
        return "software"
    if dev.device_type == VK_TYPE_DISCRETE:
        return "discrete"
    if dev.device_type == VK_TYPE_INTEGRATED:
        return "integrated"
    if dev.device_type == VK_TYPE_CPU:
        return "cpu"
    if dev.device_type == VK_TYPE_VIRTUAL:
        return "virtual"
    return "other"


def _main() -> None:
    print(json.dumps([asdict(device) for device in _enumerate_in_process()]))


if __name__ == "__main__":
    _main()
