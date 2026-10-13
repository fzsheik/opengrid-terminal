"""Workloads: allowlisted job templates, request validation and workload-aware GPU selection.

    workloads.spec          WorkloadSpec (request model), validate(body) -> Validated, WorkloadError
    workloads.templates     list_templates(), get_template(id)
    workloads.hardware_req  requirements -> eligible canonical GPUs (vendor, compute capability, VRAM, host)
    workloads.select        select(spec, who, *, now=None) -> market_best / execution_ready / blockers / estimate

Import the submodules directly (`from workloads.select import select`); this package imports nothing heavy so
it stays importable from anywhere. See methodology/workloads.md.
"""
