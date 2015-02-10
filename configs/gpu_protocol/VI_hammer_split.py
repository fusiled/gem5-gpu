# Copyright (c) 2006-2007 The Regents of The University of Michigan
# Copyright (c) 2009 Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Authors: Brad Beckmann

import math
import m5
import VI_hammer
from m5.objects import *
from m5.defines import buildEnv
from Cluster import Cluster

#
# Note: the L1 Cache latency is only used by the sequencer on fast path hits
#
class L1Cache(RubyCache):
    latency = 1

#
# Note: the L2 Cache latency is not currently used
#
class L2Cache(RubyCache):
    latency = 15

#
# Probe filter is a cache, latency is not used
#
class ProbeFilter(RubyCache):
    latency = 1

def create_system(options, system, dma_devices, ruby_system):

    if not buildEnv['GPGPU_SIM']:
        m5.util.panic("This script requires GPGPU-Sim integration to be built.")

    # Run the protocol script to setup CPU cluster, directory and DMA
    (all_sequencers, dir_cntrls, dma_cntrls, cpu_cluster) = \
                                        VI_hammer.create_system(options,
                                                                system,
                                                                dma_devices,
                                                                ruby_system)

    # If we're going to split the directories/memory controllers
    if options.num_dev_dirs > 0:
        cpu_cntrl_count = len(cpu_cluster)
    else:
        cpu_cntrl_count = len(cpu_cluster) + len(dir_cntrls)

    #
    # Create controller for the copy engine to connect to in CPU cluster
    # Cache is unused by controller
    #
    cache = L1Cache(size = "4096B", assoc = 2)

    cpu_ce_seq = RubySequencer(version = options.num_cpus + options.num_sc,
                               icache = cache,
                               dcache = cache,
                               max_outstanding_requests = 64,
                               ruby_system = ruby_system,
                               connect_to_io = False)

    cpu_ce_cntrl = GPUCopyDMA_Controller(version = 0,
                                         sequencer = cpu_ce_seq,
                                         number_of_TBEs = 256,
                                         ruby_system = ruby_system)

    cpu_cntrl_count += 1

    #
    # Build GPU cluster
    #
    gpu_cluster = Cluster(intBW = 32, extBW = 32)
    gpu_cluster.disableConnectToParent()

    l2_bits = int(math.log(options.num_l2caches, 2))
    block_size_bits = int(math.log(options.cacheline_size, 2))
    # This represents the L1 to L2 interconnect latency
    # NOTE! This latency is in Ruby (cache) cycles, not SM cycles
    per_hop_interconnect_latency = 45 # ~15 GPU cycles
    num_dance_hall_hops = int(math.log(options.num_sc, 2))
    if num_dance_hall_hops == 0:
        num_dance_hall_hops = 1
    l1_to_l2_noc_latency = per_hop_interconnect_latency * num_dance_hall_hops

    #
    # Caches for GPU cores
    #
    for i in xrange(options.num_sc):
        #
        # First create the Ruby objects associated with the GPU cores
        #
        cache = L1Cache(size = options.sc_l1_size,
                            assoc = options.sc_l1_assoc,
                            replacement_policy = "LRU",
                            start_index_bit = block_size_bits,
                            dataArrayBanks = 4,
                            tagArrayBanks = 4,
                            dataAccessLatency = 4,
                            tagAccessLatency = 4,
                            resourceStalls = False)

        l1_cntrl = GPUL1Cache_Controller(version = i,
                                  cache = cache,
                                  l2_select_num_bits = l2_bits,
                                  num_l2 = options.num_l2caches,
                                  issue_latency = l1_to_l2_noc_latency,
                                  number_of_TBEs = options.gpu_l1_buf_depth,
                                  ruby_system = ruby_system)

        gpu_seq = RubySequencer(version = options.num_cpus + i,
                            icache = cache,
                            dcache = cache,
                            max_outstanding_requests = options.gpu_l1_buf_depth,
                            ruby_system = ruby_system,
                            deadlock_threshold = 2000000,
                            connect_to_io = False)

        l1_cntrl.sequencer = gpu_seq

        exec("ruby_system.l1_cntrl_sp%02d = l1_cntrl" % i)

        #
        # Add controllers and sequencers to the appropriate lists
        #
        all_sequencers.append(gpu_seq)
        gpu_cluster.add(l1_cntrl)

        # Connect the controller to the network
        l1_cntrl.requestFromL1Cache = ruby_system.network.master
        l1_cntrl.responseToL1Cache = ruby_system.network.slave

    l2_index_start = block_size_bits + l2_bits
    # Use L2 cache and interconnect latencies to calculate protocol latencies
    # NOTE! These latencies are in Ruby (cache) cycles, not SM cycles
    l2_cache_access_latency = 30 # ~10 GPU cycles
    l2_to_l1_noc_latency = per_hop_interconnect_latency * num_dance_hall_hops
    l2_to_mem_noc_latency = 125 # ~40 GPU cycles

    l2_clusters = []
    for i in xrange(options.num_l2caches):
        #
        # First create the Ruby objects associated with this cpu
        #
        l2_cache = L2Cache(size = options.sc_l2_size,
                           assoc = options.sc_l2_assoc,
                           start_index_bit = l2_index_start,
                           replacement_policy = "LRU",
                           dataArrayBanks = 4,
                           tagArrayBanks = 4,
                           dataAccessLatency = 4,
                           tagAccessLatency = 4,
                           resourceStalls = options.gpu_l2_resource_stalls)

        l2_cntrl = GPUL2Cache_Controller(version = i,
                                L2cache = l2_cache,
                                l2_response_latency = l2_cache_access_latency +
                                                      l2_to_l1_noc_latency,
                                l2_request_latency = l2_to_mem_noc_latency,
                                ruby_system = ruby_system)

        exec("ruby_system.l2_cntrl%d = l2_cntrl" % i)
        l2_cluster = Cluster(intBW = 32, extBW = 32)
        l2_cluster.add(l2_cntrl)
        gpu_cluster.add(l2_cluster)
        l2_clusters.append(l2_cluster)

        # Connect the controller to the network
        l2_cntrl.responseToL1Cache = ruby_system.network.master
        l2_cntrl.requestFromCache = ruby_system.network.master
        l2_cntrl.responseFromCache = ruby_system.network.master
        l2_cntrl.unblockFromCache = ruby_system.network.master
        l2_cntrl.requestFromL1Cache = ruby_system.network.slave
        l2_cntrl.forwardToCache = ruby_system.network.slave
        l2_cntrl.responseToCache = ruby_system.network.slave

    gpu_phys_mem_size = system.gpu_physmem.range.size()

    if options.num_dev_dirs > 0:
        mem_module_size = gpu_phys_mem_size / options.num_dev_dirs

        #
        # determine size and index bits for probe filter
        # By default, the probe filter size is configured to be twice the
        # size of the L2 cache.
        #
        pf_size = MemorySize(options.sc_l2_size)
        pf_size.value = pf_size.value * 2
        dir_bits = int(math.log(options.num_dev_dirs, 2))
        pf_bits = int(math.log(pf_size.value, 2))
        if options.numa_high_bit:
            if options.pf_on or options.dir_on:
                # if numa high bit explicitly set, make sure it does not overlap
                # with the probe filter index
                assert(options.numa_high_bit - dir_bits > pf_bits)

            # set the probe filter start bit to just above the block offset
            pf_start_bit = block_size_bits
        else:
            if dir_bits > 0:
                pf_start_bit = dir_bits + block_size_bits - 1
            else:
                pf_start_bit = block_size_bits

        num_cpu_dirs = len(dir_cntrls)
        for i in xrange(options.num_dev_dirs):
            #
            # Create the Ruby objects associated with the directory controller
            #

            dir_version = i + num_cpu_dirs

            mem_cntrl = RubyMemoryControl(version = dir_version,
                                          bank_queue_size = 24,
                                          ruby_system = ruby_system)

            dir_size = MemorySize('0B')
            dir_size.value = mem_module_size

            pf = ProbeFilter(size = pf_size, assoc = 4,
                             start_index_bit = pf_start_bit)

            dev_dir_cntrl = Directory_Controller(version = dir_version,
                                 directory = \
                                 RubyDirectoryMemory( \
                                            version = dir_version,
                                            size = dir_size,
                                            use_map = options.use_map,
                                            map_levels = \
                                            options.map_levels,
                                            numa_high_bit = \
                                            options.numa_high_bit,
                                            device_directory = True),
                                 probeFilter = pf,
                                 memBuffer = mem_cntrl,
                                 probe_filter_enabled = options.pf_on,
                                 full_bit_dir_enabled = options.dir_on,
                                 ruby_system = ruby_system)

            if options.recycle_latency:
                dev_dir_cntrl.recycle_latency = options.recycle_latency

            exec("ruby_system.dev_dir_cntrl%d = dev_dir_cntrl" % i)
            dir_cntrls.append(dev_dir_cntrl)

            # Connect the directory controller to the network
            dir_cntrl.forwardFromDir = ruby_system.network.slave
            dir_cntrl.responseFromDir = ruby_system.network.slave
            dir_cntrl.dmaResponseFromDir = ruby_system.network.slave

            dir_cntrl.unblockToDir = ruby_system.network.master
            dir_cntrl.responseToDir = ruby_system.network.master
            dir_cntrl.requestToDir = ruby_system.network.master
            dir_cntrl.dmaRequestToDir = ruby_system.network.master
    else:
        # Since there are no device directories, use CPU directories
        # Fix up the memory sizes of the CPU directories
        num_dirs = len(dir_cntrls)
        add_gpu_mem = gpu_phys_mem_size / num_dirs
        for cntrl in dir_cntrls:
            new_size = cntrl.directory.size.value + add_gpu_mem
            cntrl.directory.size.value = new_size

    #
    # Create controller for the copy engine to connect to in GPU cluster
    # Cache is unused by controller
    #
    cache = L1Cache(size = "4096B", assoc = 2)

    gpu_ce_seq = RubySequencer(version = options.num_cpus + options.num_sc + 1,
                               icache = cache,
                               dcache = cache,
                               max_outstanding_requests = 64,
                               support_inst_reqs = False,
                               ruby_system = ruby_system,
                               connect_to_io = False)

    gpu_ce_cntrl = GPUCopyDMA_Controller(version = 1,
                                  sequencer = gpu_ce_seq,
                                  number_of_TBEs = 256,
                                  ruby_system = ruby_system)

    ruby_system.l1_cntrl_ce = gpu_ce_cntrl

    all_sequencers.append(cpu_ce_seq)
    all_sequencers.append(gpu_ce_seq)

    gpu_ce_cntrl.responseFromDir = ruby_system.network.slave
    gpu_ce_cntrl.reqToDirectory = ruby_system.network.master

    complete_cluster = Cluster(intBW = 32, extBW = 32)
    complete_cluster.add(cpu_ce_cntrl)
    complete_cluster.add(gpu_ce_cntrl)
    complete_cluster.add(cpu_cluster)
    complete_cluster.add(gpu_cluster)

    for cntrl in dir_cntrls:
        complete_cluster.add(cntrl)

    for cntrl in dma_cntrls:
        complete_cluster.add(cntrl)

    for cluster in l2_clusters:
        complete_cluster.add(cluster)

    return (all_sequencers, dir_cntrls, complete_cluster)
