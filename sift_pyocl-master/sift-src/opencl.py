#!/usr/bin/env python
#-*- coding: utf-8 -*-
#
#    Project: Sift implementation in Python + OpenCL
#             https://github.com/kif/sift_pyocl
#

"""
Automatic selection of OpenCL devices 
"""

from __future__ import division, print_function, with_statement

__authors__ = ["Jérôme Kieffer"]
__contact__ = "jerome.kieffer@esrf.eu"
__license__ = "MIT"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "2013-11-21"
__status__ = "beta"
__license__ = """
Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

"""

import os, logging
logger = logging.getLogger("sift.opencl")

try:
    import pyopencl, pyopencl.array
except ImportError:
    logger.warning("Unable to import pyOpenCl. Please install it from: http://pypi.python.org/pypi/pyopencl")
    pyopencl = None

FLOP_PER_CORE = { "GPU": 64, # GPU, Fermi at least perform 64 flops per cycle/multicore, G80 were at 24 or 48 ...
                  "CPU": 4,  # CPU, at least intel's have 4 operation per cycle
                  "ACC": 8}  # ACC: the Xeon-phi (MIC) appears to be able to process 8 Flops per hyperthreaded-core
NVIDIA_FLOP_PER_CORE = {(1, 0): 24,  # Guessed !
                         (1, 1): 24,  # Measured on G98 [Quadro NVS 295]
                         (1, 2): 24,  # Guessed !
                         (1, 3): 24,  # measured on a GT285 (GT200)
                         (2, 0): 64,  # Measured on a 580 (GF110)
                         (2, 1): 96,  # Measured on Quadro2000 GF106GL
                         (3, 0): 384, # Guessed!
                         (3, 5): 384, # Measured on K20
                         (5, 0): 256} # Maxwell 4 warps/SM 2 flops/ CU
AMD_FLOP_PER_CORE = 160  # Measured on a M7820 10 core, 700MHz 1120GFlops

class Device(object):
    """
    Simple class that contains the structure of an OpenCL device
    """
    def __init__(self, name="None", dtype=None, version=None, driver_version=None,
                 extensions="", memory=None, available=None,
                 cores=None, frequency=None, flop_core=None, idx=0, workgroup=1):
        """
        Simple container with some important data for the OpenCL device description:
        
        @param name: name of the device
        @param dtype: device type: CPU/GPU/ACC...
        @param version: driver version
        @param driver_version: 
        @param extensions: List of opencl extensions
        @param memory: maximum memory available on the device
        @param available: is the device desactivated or not 
        @param cores: number of SM/cores
        @param frequency: frequency of the device
        @param flop_cores: Flopating Point operation per core per cycle
        @param idx: index of the device within the platform
        @param workgroup: max workgroup size
        """
        self.name = name.strip()
        self.type = dtype
        self.version = version
        self.driver_version = driver_version
        self.extensions = extensions.split()
        self.memory = memory
        self.available = available
        self.cores = cores
        self.frequency = frequency
        self.id = idx
        self.max_work_group_size = workgroup
        if not flop_core:
            flop_core = FLOP_PER_CORE.get(dtype, 1)
        if cores and frequency:
            self.flops = cores * frequency * flop_core
        else:
            self.flops = flop_core


    def __repr__(self):
        return "%s" % self.name

    def pretty_print(self):
        """
        Complete device description
        
        @return: string
        """
        lst = ["Name\t\t:\t%s" % self.name,
               "Type\t\t:\t%s" % self.type,
               "Memory\t\t:\t%.3f MB" % (self.memory / 2.0 ** 20),
               "Cores\t\t:\t%s CU" % self.cores,
               "Frequency\t:\t%s MHz"%self.frequency,
               "Speed\t\t:\t%.3f GFLOPS" % (self.flops / 1000.),
               "Version\t\t:\t%s" % self.version,
               "Available\t:\t%s" % self.available]
        return os.linesep.join(lst)


class Platform(object):
    """
    Simple class that contains the structure of an OpenCL platform
    """
    def __init__(self, name="None", vendor="None", version=None, extensions=None, idx=0):
        self.name = name.strip()
        self.vendor = vendor.strip()
        self.version = version
        self.extensions = extensions.split()
        self.devices = []
        self.id = idx

    def __repr__(self):
        return "%s" % self.name

    def add_device(self, device):
        self.devices.append(device)

    def get_device(self, key):
        """
        Return a device according to key

        @param key: identifier for a device, either it's id (int) or it's name
        @type key: int or str
        """
        out = None
        try:
            devid = int(key)
        except ValueError:
            for a_dev in self.devices:
                if a_dev.name == key:
                    out = a_dev
        else:
            if len(self.devices) > devid > 0:
                out = self.devices[devid]
        return out


class OpenCL(object):
    """
    Simple class that wraps the structure ocl_tools_extended.h

    This is a static class.
    ocl should be the only instance and shared among all python modules. 
    """
    platforms = []
    nb_devices = 0
    if pyopencl:
        platform = device = pypl = devtype = extensions = pydev = None
        for idx, platform in enumerate(pyopencl.get_platforms()):
            pypl = Platform(platform.name, platform.vendor, platform.version, platform.extensions, idx)
            for idd, device in enumerate(platform.get_devices()):
                ####################################################
                # Nvidia does not report int64 atomics (we are using) ...
                # this is a hack around as any nvidia GPU with double-precision supports int64 atomics
                ####################################################
                extensions = device.extensions
                if (pypl.vendor == "NVIDIA Corporation") and ('cl_khr_fp64' in extensions):
                                extensions += ' cl_khr_int64_base_atomics cl_khr_int64_extended_atomics'
                try:
                    devtype = pyopencl.device_type.to_string(device.type).upper()
                except ValueError:
                    # pocl does not describe itself as a CPU !
                    devtype = "CPU"
                if len(devtype) > 3:
                    devtype = devtype[:3]
                if (pypl.vendor == "NVIDIA Corporation") and (devtype == "GPU") and "compute_capability_major_nv" in dir(device):
                    comput_cap = device.compute_capability_major_nv, device.compute_capability_minor_nv
                    flop_core = NVIDIA_FLOP_PER_CORE.get(comput_cap, min(NVIDIA_FLOP_PER_CORE.values()))
                elif (pypl.vendor == "Advanced Micro Devices, Inc.") and (devtype == "GPU"):
                    flop_core = AMD_FLOP_PER_CORE
                elif devtype == "CPU":
                    flop_core = FLOP_PER_CORE.get(devtype, 1)
                else:
                     flop_core = 1
                workgroup = device.max_work_group_size   
                if (devtype == "CPU") and (pypl.vendor == "Apple"):
                    logger.info("For Apple's OpenCL on CPU: enforce max_work_goup_size=1")
                    workgroup = 1
  
                pydev = Device(device.name, devtype, device.version, device.driver_version, extensions,
                               device.global_mem_size, bool(device.available), device.max_compute_units,
                               device.max_clock_frequency, flop_core, idd, workgroup)
                pypl.add_device(pydev)
                nb_devices += 1
            platforms.append(pypl)
        del platform, device, pypl, devtype, extensions, pydev


    def __repr__(self):
        out = ["OpenCL devices:"]
        for platformid, platform in enumerate(self.platforms):
            out.append("[%s] %s: " % (platformid, platform.name) + ", ".join(["(%s,%s) %s" % (platformid, deviceid, dev.name) for deviceid, dev in enumerate(platform.devices)]))
        return os.linesep.join(out)

    def get_platform(self, key):
        """
        Return a platform according

        @param key: identifier for a platform, either an Id (int) or it's name
        @type key: int or str
        """
        out = None
        try:
            platid = int(key)
        except ValueError:
            for a_plat in self.platforms:
                if a_plat.name == key:
                    out = a_plat
        else:
            if len(self.platforms) > platid > 0:
                out = self.platforms[platid]
        return out

    def select_device(self, dtype="ALL", memory=None, extensions=[], best=True, **kwargs):
        """
        Select a device based on few parameters (at the end, keep the one with most memory)

        @param type: "gpu" or "cpu" or "all" ....
        @param memory: minimum amount of memory (int)
        @param extensions: list of extensions to be present
        @param best: shall we look for the
        """
        if "type" in kwargs:
            dtype = kwargs["type"].upper()
        else:
            dtype = dtype.upper()
        if len(dtype) > 3:
            dtype = dtype[:3]
        best_found = None
        for platformid, platform in enumerate(self.platforms):
            for deviceid, device in enumerate(platform.devices):
                if (dtype in ["ALL", "DEF"]) or (device.type == dtype):
                    if (memory is None) or (memory <= device.memory):
                        found = True
                        for ext in extensions:
                            if ext not in device.extensions:
                                found = False
                        if found:
                            if not best:
                                return platformid, deviceid
                            else:
                                if not best_found:
                                    best_found = platformid, deviceid, device.flops
                                elif best_found[2] < device.flops:
                                    best_found = platformid, deviceid, device.flops
        if best_found:
            return  best_found[0], best_found[1]

    def create_context(self, devicetype="ALL", useFp64=False, platformid=None, deviceid=None):
        """
        Choose a device and initiate a context.

        Devicetypes can be GPU,gpu,CPU,cpu,DEF,ACC,ALL.
        Suggested are GPU,CPU.
        For each setting to work there must be such an OpenCL device and properly installed.
        E.g.: If Nvidia driver is installed, GPU will succeed but CPU will fail. The AMD SDK kit is required for CPU via OpenCL.
        @param devicetype: string in ["cpu","gpu", "all", "acc"]
        @param useFp64: boolean specifying if double precision will be used
        @param platformid: integer
        @param devid: integer
        @return: OpenCL context on the selected device
        """
        if (platformid is not None) and (deviceid is not None):
            platformid = int(platformid)
            deviceid = int(deviceid)
        else:
            if useFp64:
                ids = ocl.select_device(type=devicetype, extensions=["cl_khr_int64_base_atomics"])
            else:
                ids = ocl.select_device(dtype=devicetype)
            if ids:
                platformid = ids[0]
                deviceid = ids[1]
        if (platformid is not None) and  (deviceid is not None):
            ctx = pyopencl.Context(devices=[pyopencl.get_platforms()[platformid].get_devices()[deviceid]])
        else:
            logger.warn("Last chance to get an OpenCL device ... probably not the one requested")
            ctx = pyopencl.create_some_context(interactive=False)
        return ctx


def release_cl_buffers(cl_buffers):
    """
    @param cl_buffer: the buffer you want to release
    @type cl_buffer: dict(str, pyopencl.Buffer)

    This method release the memory of the buffers store in the dict
    """
    for key in cl_buffers:
        if cl_buffers[key] is not None:
            try:
                cl_buffers[key].release()
                cl_buffers[key] = None
            except pyopencl.LogicError:
                logger.error("Error while freeing buffer %s", key)
    return cl_buffers


def allocate_cl_buffers(buffers, device, context):
    """
    @param buffers: the buffers info use to create the pyopencl.Buffer
    @type buffer: list(std, flag, numpy.dtype, int)
    @return: a dict containing the instanciated pyopencl.Buffer
    @rtype: dict(str, pyopencl.Buffer)

    This method instanciate the pyopencl.Buffer from the buffers
    description.
    """
    mem = {}

    # check if enough memory is available on the device
    ualloc = 0
    for _, _, dtype, size in buffers:
        ualloc += numpy.dtype(dtype).itemsize * size
    memory = device.memory
    logger.info("%.3fMB are needed on device which has %.3fMB",
                ualloc / 1.0e6, memory / 1.0e6)
    if ualloc >= memory:
        raise MemoryError("Fatal error in _allocate_buffers. Not enough device memory for buffers (%lu requested, %lu available)" % (ualloc, memory))  # noqa

    # do the allocation
    try:
        for name, flag, dtype, size in buffers:
            mem[name] = \
                pyopencl.Buffer(context, flag,
                                numpy.dtype(dtype).itemsize * size)
    except pyopencl.MemoryError as error:
        release_cl_buffers(mem)
        raise MemoryError(error)

    return mem

if pyopencl:
    ocl = OpenCL()
    if ocl.nb_devices == 0:
        ocl = None
else:
    ocl = None

