"""CUDA-specific parse carrier. CUDA reuses every C++ Raw dataclass (RawType/RawFunction/
RawMethod/RawCall/...) unchanged -- the extraction is identical because the grammar is a superset.
This subclass just rides two extra side-cars through the (picklable) parse->resolve boundary:

  * exec_spaces: {(name, line_start): space}  -- the execution space of each __global__/__device__/
    __host__ function or method, keyed so resolution can match it to the Function/Method node the
    shared C++ resolver emits (which carries the same name + line_start).
  * launches:    [(caller_name, caller_line_start, callee_name)]  -- each `kernel<<<...>>>(args)`
    launch site, as (the enclosing function, the launched kernel's simple name); resolution turns
    these into LAUNCHES edges.

Both are plain built-ins (dict/list of str/int tuples) so the object stays picklable for the
ProcessPoolExecutor parse, exactly like the base RawFile."""

from __future__ import annotations

from dataclasses import dataclass, field

from auryn.ingestors.cpp.raw_model import RawFile as CppRawFile


@dataclass
class CudaRawFile(CppRawFile):
    # (name, line_start) -> "global" | "device" | "host" | "host_device"
    exec_spaces: dict = field(default_factory=dict)
    # (caller_name, caller_line_start, callee_name)
    launches: list = field(default_factory=list)
