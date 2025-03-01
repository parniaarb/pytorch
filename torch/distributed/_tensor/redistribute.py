# Copyright (c) Meta Platforms, Inc. and affiliates
from functools import lru_cache
from typing import cast, Dict, List, NamedTuple, Tuple

import torch
import torch.distributed._tensor.api as dtensor
from torch.distributed._tensor.device_mesh import DeviceMesh
from torch.distributed._tensor.placement_types import (
    _Partial,
    DTensorSpec,
    Placement,
    Replicate,
    Shard,
)


class _TransformInfo(NamedTuple):
    mesh_dim: int
    src_dst_placements: Tuple[Placement, Placement]
    # logical_shape on this mesh dimension
    logical_shape: List[int]


def _replicate_then_shard(val: _TransformInfo) -> int:
    """
    This is a helper function to allow reordering _TransformInfo list. The high level
    idea is that we want to reorder the sharding redistributions so that the DTensor
    redistribution is consistent with its full tensor. This is built on top of two simple
    assumptions:
    1. Replication happens from inner to outer dimension. i.e. Shard -> Replicate
    2. Sharding happens from outer to inner dimension, i.e. Replicate -> Shard

    So we always put the replication first and put sharding later.
    """
    mesh_dim = val.mesh_dim
    src, dst = val.src_dst_placements
    if (dst.is_replicate() or dst.is_partial()) and src.is_shard():
        return -mesh_dim
    elif (src.is_replicate() or src.is_partial()) and dst.is_shard():
        return mesh_dim
    else:
        return 0


@lru_cache(maxsize=None)
def _gen_transform_infos(
    src_spec: DTensorSpec,
    dst_spec: DTensorSpec,
) -> List[_TransformInfo]:
    """
    Generate the transform infos from the source placements to the target placements, to
    transform from source to target placement it might have multipl steps, i.e. it might
    decompose Si -> Sj into Si -> R -> Sj.
    This would detects if there're mis-aligned shardings between src/dst placements.
    i.e. (Shard(0), Shard(0)) -> (Replicate(), Shard(0)), in this case Shard(0) -> Shard(0)
    for mesh dimension 1 actually needs reshard, because in the first case it's a sub-sharding
    of an already tensor dimension 0, and in the second case, it's the first sharding on tensor
    dimension 0.

    Note that we also currently handles sharding on different tensor dimensions, e.g.
    Shard(0) -> Shard(1) in this pass
    """
    src_dim_counts: Dict[int, int] = {}
    dst_dim_counts: Dict[int, int] = {}
    transform_infos: List[_TransformInfo] = []

    src_placements = src_spec.placements
    dst_placements = dst_spec.placements
    device_mesh = src_spec.device_mesh
    my_coordinate = device_mesh.get_coordinate()
    assert my_coordinate is not None

    # logical shape records the logic tensor shape on the mesh dimension
    # this is useful to ensure uneven sharding gets correct output shape
    initial_logical_shape = list(src_spec.shape)
    mesh_dims_to_logical_shape = [initial_logical_shape]
    mesh_ndim = len(src_placements)

    for i, (src, dst) in enumerate(zip(src_placements, dst_placements)):
        # detect mis-aligned sharding and build logical shapes
        current_logical_shape = mesh_dims_to_logical_shape[i]
        if isinstance(src, Shard):
            src_dim_counts[src.dim] = src_dim_counts.get(src.dim, 0) + 1

            if i < mesh_ndim - 1:
                # calculate and save the logical shape for this sharding
                mesh_dim_size = device_mesh.size(mesh_dim=i)
                local_shard_size, _ = src._local_shard_size_on_dim(
                    current_logical_shape[src.dim],
                    mesh_dim_size,
                    my_coordinate[i],
                )
                new_logical_shape = list(current_logical_shape)
                new_logical_shape[src.dim] = local_shard_size
                mesh_dims_to_logical_shape.append(new_logical_shape)
        else:
            mesh_dims_to_logical_shape.append(current_logical_shape)

        if isinstance(dst, Shard):
            dst_dim_counts[dst.dim] = dst_dim_counts.get(dst.dim, 0) + 1

        if (
            isinstance(src, Shard)
            and isinstance(dst, Shard)
            and (
                src.dim != dst.dim or src_dim_counts[src.dim] != dst_dim_counts[dst.dim]
            )
        ):
            # decompose Shard(i) -> Shard(j) into Shard(i) -> Replicate() -> Shard(j)
            transform_infos.append(
                _TransformInfo(
                    mesh_dim=i,
                    src_dst_placements=(src, Replicate()),
                    logical_shape=mesh_dims_to_logical_shape[i],
                )
            )
            transform_infos.append(
                _TransformInfo(
                    mesh_dim=i,
                    src_dst_placements=(Replicate(), dst),
                    logical_shape=mesh_dims_to_logical_shape[i],
                )
            )
        else:
            transform_infos.append(
                _TransformInfo(
                    mesh_dim=i,
                    src_dst_placements=(src, dst),
                    logical_shape=mesh_dims_to_logical_shape[i],
                )
            )

    # sort the pairs by first perform replication then sharding
    transform_infos.sort(key=_replicate_then_shard)
    return transform_infos


def redistribute_local_tensor(
    local_tensor: torch.Tensor,
    current_spec: DTensorSpec,
    target_spec: DTensorSpec,
    is_backward: bool = False,
) -> torch.Tensor:
    """
    This redistribute the local tensor (torch.Tensor) from the current DTensorSpec to
    the target DTensorSpec, which involves the necessary collective calls to transform
    the local shard of the DTensor from its current spec to the target spec.
    """

    if current_spec.mesh != target_spec.mesh:
        # TODO: alltoall/permute reshuffling to change device_mesh if they are not the same
        raise NotImplementedError("Cross device mesh comm not supported yet!")

    new_local_tensor = None
    device_mesh = current_spec.mesh

    my_coordinate = device_mesh.get_coordinate()

    if my_coordinate is None:
        # if rank is not part of mesh, we skip redistribute and simply return local_tensor,
        # which should be an empty tensor
        return local_tensor

    transform_infos = _gen_transform_infos(current_spec, target_spec)

    for transform_info in transform_infos:
        i = transform_info.mesh_dim
        current, target = transform_info.src_dst_placements
        num_chunks = device_mesh.size(mesh_dim=i)

        if current == target:
            # short cut, just use the original local tensor
            new_local_tensor = local_tensor
            continue

        if target.is_replicate():
            # Case 1: target is Replicate
            if current.is_partial():
                partial_spec = cast(_Partial, current)
                new_local_tensor = partial_spec._to_replicate(
                    local_tensor, device_mesh, i
                )
            elif current.is_shard():
                current_placement = cast(Shard, current)
                new_local_tensor = current_placement._to_replicate_tensor(
                    local_tensor, device_mesh, i, transform_info.logical_shape
                )
            else:
                raise RuntimeError(
                    f"redistribute from {current} to {target} not supported yet"
                )
        elif target.is_shard():
            # Case 2: target is Shard
            target_placement = cast(Shard, target)
            target_dim = target_placement.dim
            if current.is_partial():
                partial_spec = cast(_Partial, current)
                new_local_tensor = partial_spec._to_shard(
                    local_tensor, device_mesh, i, target_placement
                )
            elif current.is_replicate():
                # split the tensor and return the corresponding cloned local shard
                shards, _ = target_placement._split_tensor(
                    local_tensor,
                    num_chunks,
                    with_padding=False,
                    contiguous=False,
                )
                new_local_tensor = shards[my_coordinate[i]].clone()
            else:
                # NOTE: we don't support this case efficiently yet, the fallback path we are going here is
                # to decompose Shard(0) -> Shard(1) into Shard(0) -> Replicate -> Shard(1)
                # TODO: enable this with all_to_all
                assert (
                    current.is_shard()
                ), f"Current placement should be shard but found {current}"
                shard_spec = cast(Shard, current)
                if shard_spec.dim != target_placement.dim:
                    new_local_tensor = shard_spec._to_replicate_tensor(
                        local_tensor, device_mesh, i, transform_info.logical_shape
                    )
                    shards, _ = target_placement._split_tensor(
                        new_local_tensor,
                        num_chunks,
                        with_padding=False,
                        contiguous=False,
                    )
                    new_local_tensor = shards[my_coordinate[i]]
        elif target.is_partial():
            if current.is_replicate():
                # For replicate -> partial forward pass we perform division to num of chunks
                # and generate parial, and recover it back when pending sum get cleared.
                # Skip/pass through when replicate -> partial is in backward pass.
                new_local_tensor = (
                    local_tensor / num_chunks if not is_backward else local_tensor
                )
            else:
                raise RuntimeError(
                    f"redistribute from {current} to {target} not supported yet"
                )

        assert new_local_tensor is not None
        local_tensor = new_local_tensor

    assert new_local_tensor is not None, "redistribute failed!"

    return new_local_tensor


class Redistribute(torch.autograd.Function):
    @staticmethod
    def forward(  # type: ignore[override]
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        input: "dtensor.DTensor",
        device_mesh: DeviceMesh,
        placements: Tuple[Placement, ...],
    ):
        current_spec = input._spec
        ctx.current_spec = current_spec
        target_spec = DTensorSpec(
            device_mesh, placements, tensor_meta=input._spec.tensor_meta
        )

        local_tensor = input._local_tensor
        output = redistribute_local_tensor(local_tensor, current_spec, target_spec)

        return dtensor.DTensor(
            output,
            device_mesh,
            target_spec.placements,
            shape=input.shape,
            dtype=input.dtype,
            requires_grad=input.requires_grad,
            stride=input.stride(),
        )

    @staticmethod
    def backward(ctx, grad_output: "dtensor.DTensor"):  # type: ignore[override]
        previous_spec = ctx.current_spec
        # When we run backward pass of redistribute (i.e. manual redistribute from
        # user code instead of torch_dispatch), we scan first and see if we need
        # to change the target placement for one special case:
        #   replicate -> partial.
        # In this case we keep the grad as replicate, this is because we don't
        # want to convert the replicated gradients back to partial, although
        # that's logically conform with the same layout, converting the gradients
        # back to partial is actually useless as you would have to do reduce later
        # which would be more expensive than keeping it replicate! For this reason,
        # we keep the replicate grad here.
        # TODO: see if this make sense for all cases.
        current_spec = grad_output._spec

        target_placements: List[Placement] = []
        for current, target in zip(current_spec.placements, previous_spec.placements):
            if not current.is_partial() and target.is_partial():
                # keep target placement to replicate instead of partial in this case
                target_placements.append(Replicate())
            else:
                target_placements.append(target)
        target_spec = DTensorSpec(
            previous_spec.mesh,
            tuple(target_placements),
            tensor_meta=previous_spec.tensor_meta,
        )

        local_tensor = grad_output._local_tensor
        output = redistribute_local_tensor(local_tensor, current_spec, target_spec)
        output_dtensor = dtensor.DTensor(
            output,
            target_spec.mesh,
            target_spec.placements,
            shape=grad_output.shape,
            dtype=grad_output.dtype,
            requires_grad=grad_output.requires_grad,
            stride=grad_output.stride(),
        )

        return (
            output_dtensor,
            None,
            None,
        )
