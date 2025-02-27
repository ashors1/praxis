# coding=utf-8
# Copyright 2022 The Pax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base class for the Praxis layers."""

from __future__ import annotations

import collections
import copy
import dataclasses
import enum
import functools
import itertools
import math
import typing
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type, TypeVar, Union, Mapping

from absl import flags
from absl import logging
import fiddle as fdl
from fiddle import daglish
from flax import core as flax_core
from flax import linen as nn
from flax import struct
import jax
from jax import numpy as jnp
from jax import random as jrandom
import numpy as np
from praxis import asserts
from praxis import base_hyperparams
from praxis import pax_fiddle
from praxis import py_utils
from praxis import pytypes

FLAGS = flags.FLAGS

NestedMap = py_utils.NestedMap

ParamsT = pytypes.HParamsT
BaseLayerT = TypeVar('BaseLayerT', bound='BaseLayer')
JTensor = pytypes.JTensor
PRNGKey = pytypes.PRNGKey
JTensorOrPartitionSpec = pytypes.JTensorOrPartitionSpec
NpTensor = pytypes.NpTensor
SummaryDict = pytypes.SummaryDict

BaseHyperParams = base_hyperparams.BaseHyperParams
BaseParameterizable = base_hyperparams.BaseParameterizable
InstantiableHyperParams = base_hyperparams.InstantiableHyperParams
sub_config_field = base_hyperparams.sub_config_field
template_field = pax_fiddle.template_field
instance_field = pax_fiddle.instance_field

Nested = pytypes.Nested
NestedJTensor = pytypes.NestedJTensor
NestedBool = pytypes.NestedBool
NestedHParams = pytypes.NestedHParams
NestedJTensorOrPartitionSpec = pytypes.NestedJTensorOrPartitionSpec
NestedPartitionSpec = pytypes.NestedPartitionSpec

SplitDimsMapping = pytypes.SplitDimsMapping

AxisMetadata = flax_core.meta.AxisMetadata
TAxisMetadata = flax_core.meta.TAxisMetadata

# Layer stack to establish parent child relationships.
_LAYER_STACK = py_utils.ThreadLocalStack()

# Global state that may impact how certain jax computation will be carried (e.g.
# whether or not to enable dropout).
_JaxContextStack = py_utils.ThreadLocalStack()

# A few special Flax Linen variable collection names.
PARAMS = 'params'
AUX_LOSS = 'aux_loss'
SUMMARIES = 'summaries'
NON_TRAINABLE = 'non_trainable'
DECODE_CACHE = 'decoder_cache'
PREFIX_DECODE_CACHE = 'prefix_decoder_cache'
INTERMEDIATES = 'intermediates'

# hyper-params used to construct a layer.
HYPER_PARAMS = 'hyper_params'

# Used for interoperability with Flax-based libraries and
# not for use within Pax' own layers.
# It will be handled as NON_TRAINABLE in train mode.
NON_PAX_VAR_COLLECTION = ['batch_stats', 'params_axes']

# Only allow PARAMS and NON_TRAINABLE to be mutable in order to allow Repeat
# and Pipeline layer __call__ to alter the shape of SUMMARIES collection, e.g.
# by splitting along scan axis using nn.map_variable.
# The goal is to allow init_vars = layer.init(...) to be fed into
# layer(init_vars, ...).
DEFAULT_INIT_MUTABLE_LIST = [PARAMS, NON_TRAINABLE] + NON_PAX_VAR_COLLECTION

# A few special Flax RNG stream names.
RANDOM = 'random'
NON_PAX_RNG_KEY = 'dropout'

# Postfix for quantized scale and zero point names.
QUANTIZED_SCALE_NAME_POSTFIX = '_quantized_scale'
QUANTIZED_ZP_NAME_POSTFIX = '_quantized_zp'

# Public aliase of base_hyperparams.instantiate() for convenience.
instantiate = base_hyperparams.instantiate

# A function that transforms a decode state variable of a layer. It has three
# inputs: (state tensor, batch dim, time dim). It returns the transformed state.
# The transformation is done on the batch and/or time dimension; if a required
# dimension is missing (represented as -1), the function should not change the
# state.
DecodeStateTransformFn = Callable[[JTensor, int, int], JTensor]

# The axis name that is pmmaped over.
PMAP_PARALLEL_AXIS_NAME = 'batch'


# Whether caller is running under pmap(..., axis_name=PMAP_PARALLEL_AXIS_NAME).
# Note that if the pmap doesn't specify `axis_name=PMAP_PARALLEL_AXIS_NAME`,
# this function returns False.
# For example,
#   def f(x):
#     return is_running_under_pmap()
#
#   jax.pmap(f)(jnp.ones((1,)))
#     = ShardedDeviceArray([ False], dtype=bool)
#   jax.pmap(f, axis_name=PMAP_PARALLEL_AXIS_NAME)(jnp.ones((1,)))
#     = ShardedDeviceArray([ True], dtype=bool)
def is_running_under_pmap() -> bool:
  """Whether currently running under pmap with PMAP_PARALLEL_AXIS_NAME."""
  try:
    _ = jax.lax.axis_index(PMAP_PARALLEL_AXIS_NAME)
    return True
  except NameError:
    return False


class WeightHParamsCollection:
  """Weight hparams collection annotation.

  Weight hparams collections annotates variables with special properties,
  e.g. whether or not the variable is leanable, whether or not the variable is
  subject to lp regularization.
  """
  SKIP_LP_REGULARIZATION = '__lingvo_jax_skip_regularization'
  NON_TRAINABLE = '_lingvo_jax_non_trainable'
  REQUIRES_MEAN_SYNC = '_requires_mean_sync'
  REQUIRES_SUM_SYNC = '_requires_sum_sync'
  DISALLOW_BFLOAT16_CONVERSION = '_disallow_bfloat16_conversion'


def var_not_trainable(var_hparams: ParamsT) -> bool:
  """Returns True if var_hparams is not a trainable variable."""
  return WeightHParamsCollection.NON_TRAINABLE in var_hparams.collections


def var_requires_mean_sync(var_hparams: ParamsT) -> bool:
  """Returns True if var_hparams requires synchronization across replicas."""
  return WeightHParamsCollection.REQUIRES_MEAN_SYNC in var_hparams.collections


def var_requires_sum_sync(var_hparams: ParamsT) -> bool:
  """Returns True if var_hparams requires summation across replicas."""
  return WeightHParamsCollection.REQUIRES_SUM_SYNC in var_hparams.collections


def var_disallow_bfloat16_conversion(var_hparams: ParamsT) -> bool:
  """Returns True if var_hparams excludes from bfloat16 conversion."""
  return (WeightHParamsCollection.DISALLOW_BFLOAT16_CONVERSION
          in var_hparams.collections)


def var_skip_lp_regularization(var_params: ParamsT) -> bool:
  return (
      WeightHParamsCollection.SKIP_LP_REGULARIZATION in var_params.collections
  )


def to_partition_spec(
    split_dims_mapping: SplitDimsMapping, mesh_axis_names: Sequence[str]
) -> jax.sharding.PartitionSpec:
  """Converts split_dims_mapping to jax.sharding.PartitionSpec.

  Args:
    split_dims_mapping: A (nested) tuple of mesh axis to split x over. Below are
      a few example sharding specifications. (0, 2)  - the first dim of x is
      split over the first axis of the mesh and the second dim over the third
      axis of the mesh. (1, -1) - the first dim of x is split over the second
      axis of the mesh and the second dim is replicated. (1, None) - the first
      dim is split over the second axis of the mesh, and the second dim
      replicated. ('data', 'mdl') - the first dim is split over the 'data' axis
      of the mesh and the second dim over the 'mdl' axis. (('replica', 'data'),
      'mdl'), the first dim is split over both the 'replica' and 'data' axes,
      while the second dim over the 'mdl' axis.
    mesh_axis_names: A tuple/list of strings of the name of the device mesh.

  Returns:
    A jax.sharding.PartitionSpec.
  """

  def _parse_split_dims(dims_mapping):
    split_dims = []

    for s_i in dims_mapping:
      if isinstance(s_i, int):
        if s_i < 0:
          split_dims.append(None)
        else:
          assert s_i < len(mesh_axis_names), (
              f's_i: {s_i}, mesh_axis_names: {mesh_axis_names}')
          split_dims.append(mesh_axis_names[s_i])
      elif isinstance(s_i, str):
        assert s_i in mesh_axis_names
        split_dims.append(s_i)
      elif isinstance(s_i, (tuple, list)):
        split_dims.append(_parse_split_dims(s_i))
      else:
        assert s_i is None
        split_dims.append(None)

    return tuple(split_dims)

  partition_spec = _parse_split_dims(split_dims_mapping)
  return jax.sharding.PartitionSpec(*partition_spec)


def var_partition_specs(
    var_specs: NestedHParams, mesh_shape: Sequence[int],
    device_axis_names: List[str]) -> NestedJTensorOrPartitionSpec:
  """Given variable specs (WeightHParams), returns pjit partition specs.

  Args:
    var_specs: A nested structure of WeightHParams.
    mesh_shape: Shape of logical mesh.
    device_axis_names: Axis name for each mesh axis.

  Returns:
    A nested structure of PartitionSpec.
  """

  assert len(device_axis_names) == len(mesh_shape)

  def _get_spec(var_p):
    v_shape = var_p.shape
    # v_split_dim_mapping may contain a mixture of -1, integers, str, or None.
    # -1 and None both indicates that the corresponding dim is not partitioned.
    v_split_dim_mapping = var_p.tensor_split_dims_mapping
    if v_split_dim_mapping is not None:
      assert len(v_split_dim_mapping) == len(v_shape)
    else:
      v_split_dim_mapping = [-1] * len(v_shape)

    if var_p.repeat_prefix is not None:
      repeat_prefix = var_p.repeat_prefix
      if var_p.repeat_prefix_split_dims_mapping is not None:
        prefix_split_dims_mapping = var_p.repeat_prefix_split_dims_mapping
        assert len(prefix_split_dims_mapping) == len(repeat_prefix)
      else:
        prefix_split_dims_mapping = [-1] * len(repeat_prefix)
      # Append sharding annotations for the prefix part.
      v_split_dim_mapping = (
          list(prefix_split_dims_mapping) + list(v_split_dim_mapping))

    return to_partition_spec(v_split_dim_mapping, device_axis_names)

  return jax.tree_map(_get_spec, var_specs)


def maybe_shard(x: JTensor,
                split_dims_mapping: SplitDimsMapping = None,
                mesh_axis_names: Optional[Sequence[str]] = None,
                unconstrained_dims: Optional[Sequence[int]] = None) -> JTensor:
  """Adds explicit xla sharding constraints.

  This is a wrap around jax.with_sharding_constraint to allow for adding
  explicit sharding annotations to an intermediate node in a jax function.

  No sharding annotation is added if either split_dims_mapping is None or
  mesh_axis_names is None.

  If mesh_axes_transpose exists in the current context, device axes will be
  remapped according to the transpose rules.

  Args:
    x: the input tensor to be sharded.
    split_dims_mapping: A (nested) tuple of mesh axis to split x over. Below are
      a few example sharding specifications. (0, 2) - in this case, the first
      dim of x is split over the first axis of the mesh and the second dim over
      the third axis of the mesh. (1, -1) - in this case, the first dim of x is
      split over the second axis of the mesh and the second dim is replicated.
      (1, None) - First dim is split over the second dim of the mesh, and the
      second dim replicated. ('data', 'mdl') - in this case,  the first dim is
      split over the 'data' axis of the mesh and the second dim over the 'mdl'
      axis. (('replica', 'data'), 'mdl'), in this case the first dim is split
      over both the 'replica' and 'data' axes, while the second dim over the
      'mdl' axis.
    mesh_axis_names: A tuple/list of strings of the name of the device mesh.
    unconstrained_dims: A tuple/list of dimensions for which the sharding will
      be determined by XLA (sharding propagation). We allow this only for this
      internal annotation function, not for the program inputs/outputs.

  Returns:
    An annotated JTensor.
  """
  if split_dims_mapping is None or mesh_axis_names is None:
    return x

  assert len(x.shape) == len(split_dims_mapping), (
      'Invalid split_dims_mapping. Expected len(split_dims_mapping) '
      f'is {len(x.shape)}, while it is {len(split_dims_mapping)}. '
      f'x.shape = {x.shape} and split_dims_mapping = {split_dims_mapping}'
  )
  partition_spec = to_partition_spec(split_dims_mapping, mesh_axis_names)

  if JaxContext.has_context():
    mapping = cur_jax_context().hparams.mesh_axes_transpose
    if mapping:

      def _transpose_one_dim(axes):
        if axes is None:
          return axes
        if isinstance(axes, str):
          return mapping.get(axes, axes)
        return tuple([_transpose_one_dim(x) for x in axes])

      partition_spec = jax.sharding.PartitionSpec(
          *[_transpose_one_dim(x) for x in partition_spec]
      )

  if unconstrained_dims is not None:
    partition_spec_list = list(partition_spec)
    for dim in unconstrained_dims:
      partition_spec_list[dim] = partition_spec.UNCONSTRAINED
    partition_spec = jax.sharding.PartitionSpec(*partition_spec_list)

  return py_utils.with_sharding_constraint(x, partition_spec)


class WeightInit(BaseHyperParams):
  """Static class providing weight initialization config params.

  Attributes:
    method: Initialization method.
    scale: Initialization scale.
  """
  method: str
  scale: float

  @pax_fiddle.auto_config
  @staticmethod
  def Gaussian(scale: float = 1.0):
    """scale * jax.random.normal(0, 1.0)."""
    return WeightInit('gaussian', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def Uniform(scale: float = 1.0):
    """scale * jax.random.uniform(-1.0, 1.0)."""
    return WeightInit('uniform', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def Xavier(scale: float = 1.0):
    """Xavier initialization (x = sqrt(6. / (in + out)); [-x, x])."""
    return WeightInit('xavier', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def XavierWithFixupParams(scale: float = 1.0,
      depth: float = 1.0,
                            layers_per_residual_block: float = 1.0):
    """Xavier initialization with Fixup."""
    scale = scale * math.pow(depth, (-1.0 / (2 * layers_per_residual_block)))
    return WeightInit('xavier', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def Constant(scale: float = 1.0):
    """scale."""
    return WeightInit('constant', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def TruncatedGaussian(scale: float = 1.0):
    """scale * jax.random.truncated_normal(-2.0, 2.0)."""
    return WeightInit('truncated_gaussian', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def GaussianSqrtDim(scale: float = 1.0):
    """scale * jax.random.normal(0, 1 / sqrt(dim0))."""
    return WeightInit('gaussian_sqrt_dim', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def GaussianSqrtFanIn(scale: float = 1.0):
    """scale * jax.random.normal(0, 1 / sqrt(fan_in))."""
    return WeightInit('gaussian_sqrt_fanin', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def GaussianSqrtFanOut(scale: float = 1.0):
    """scale * jax.random.normal(0, 1 / sqrt(fan_out))."""
    return WeightInit('gaussian_sqrt_fanout', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def GaussianSqrtFanAvg(scale: float = 1.0):
    """jax.random.normal(0, sqrt(2.0 / (in + out)))."""
    return WeightInit('gaussian_sqrt_fanavg', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def UniformSqrtDim(scale: float = 1.0):
    """scale * jax.random.uniform(-1 / sqrt(dim0), 1 / sqrt(dim0))."""
    return WeightInit('uniform_sqrt_dim', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def UniformSqrtFanAvg(scale: float = 1.0):
    """sqrt(6 * scale / (in + out)) * jax.random.uniform(-1, 1)."""
    return WeightInit('uniform_sqrt_fanavg', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def UniformUnitScaling(scale: float = 1.0):
    """scale * sqrt(3) / sqrt(dim0) * jax.random.uniform(-1, 1)."""
    return WeightInit('uniform_unit_scaling', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def TruncatedGaussianSqrtDim(scale: float = 1.0):
    """scale * jax.random.truncated_normal(0, 1 / sqrt(dim0))."""
    return WeightInit('truncated_gaussian_sqrt_dim', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def TruncatedGaussianSqrtFanIn(scale: float = 1.0):
    """scale * jax.random.truncated_normal(0, 1 / sqrt(fan_in))."""
    return WeightInit('truncated_gaussian_sqrt_fanin', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def TruncatedGaussianSqrtFanOut(scale: float = 1.0):
    """scale * jax.random.truncated_normal(0, 1 / sqrt(fan_out))."""
    return WeightInit('truncated_gaussian_sqrt_fanout', scale)

  @pax_fiddle.auto_config
  @staticmethod
  def ScaledDeltaOrthogonal(scale: float = 1.0):
    return WeightInit('delta_orthogonal', scale)


_DEFAULT_XAVIER_INIT = 1.000001


@pax_fiddle.auto_config
def default_param_init():
  # Here we use 1.000001 as a signature for user picking up the
  # default param initializer.
  return WeightInit.Xavier(_DEFAULT_XAVIER_INIT)


def is_default_param_init(p: Union[WeightInit, pax_fiddle.Config[WeightInit]]):
  return p.method == 'xavier' and abs(p.scale - _DEFAULT_XAVIER_INIT) < 1e-7


class WeightHParams(BaseHyperParams):
  """Hyperparams for a weight variable specifying shape/init/dtype etc.

  Attributes:
    shape: The weight shape.
    init: The initialization method.
    dtype: The weight data type.
    collections: Variable collections this weight belongs to.
    mesh_shape: Shape of logical mesh. mesh_shape and tensor_split_dims_mapping
      below together specifies how this weight tensor should be sharded across
      different tpu cores. If None, this variable is not sharded. Here are
      examples of mesh shape: [2, 3, 4] for 2-way replica parallelism, 3-way
      data parallelism and 4-way model parallelism.
    tensor_split_dims_mapping: A list of integers that map each tensor axis to
      the device mesh axis along which it is sharded. Its length is the tensor
      rank, and split_dims_mapping[i] is device mesh axis for tensor dimension
      i. Use -1 for tensor dimensions that are not sharded. If the list is set
      to None and a mesh_shape is specified, the sharding will be treated as
      replicated. Here is a concrete examples: mesh_shape=[2, 4] and shape=[x,
      y, z], so this is a 3d variable. tensor_split_dims_mapping=[-1, -1, 1], in
      this case, the third dim of the variable is split along the second dim of
      the mesh. Each split of the variable is of the shape [x, y, z/4].
    repeat_prefix: If not None, the full shape of this var is
      repeat_prefix+shape. For example, if repeat_prefix=[16, 2], and
      shape=[512, 1024], then real shape of variable is [16, 2, 512, 1024].
      "repeat_prefix" is often used if a layer is to be used in a recurrent
      loop, where logically there are n sub-layers, but for performance/hbm
      usage reasons we stack all the variables in creating those n-layers.
    repeat_prefix_split_dims_mapping: Tensor split dims mapping for the
      repeat_prefix dims.
    repeat_optimizer_dims_mapping: Tensor split dims mapping used for the
      optimizer state variables corresponding to the repeat prefix dims.
    fan_in_axes: Shape axes used to compute fan in for Xavier init and
      gaussian_sqrt_(fanin|fanout) init variants.
    fan_out_axes: Shape axes used to compute fan out for Xavier init and
      gaussian_sqrt_(fanin|fanout) init variants.
  """
  shape: Sequence[int]
  init: Optional[WeightInit] = None
  dtype: Optional[jnp.dtype] = None
  collections: Optional[Sequence[str]] = None
  mesh_shape: Optional[Sequence[int]] = None
  tensor_split_dims_mapping: SplitDimsMapping = None
  repeat_prefix: Optional[Sequence[int]] = None
  repeat_prefix_split_dims_mapping: SplitDimsMapping = None
  repeat_optimizer_dims_mapping: SplitDimsMapping = None
  fan_in_axes: Optional[Sequence[int]] = None
  fan_out_axes: Optional[Sequence[int]] = None

  # If any kwargs are None, they are given defaults from the parent BaseLayer
  # in self.create_variable.
  def __post_init__(self):
    if self.collections is None:
      self.collections = []
    if self.mesh_shape is not None:
      if self.tensor_split_dims_mapping is None:
        self.tensor_split_dims_mapping = (-1,) * len(self.shape)
        logging.info(
            'Sets tensor_split_dims_mapping of a param of shape %s to %s',
            self.shape, self.tensor_split_dims_mapping)
      assert len(self.tensor_split_dims_mapping) == len(self.shape)


NestedWeightHParams = Nested[WeightHParams]


def get_fan_in_fan_out(
    shape: Sequence[int],
    fan_in_axes: Optional[Sequence[int]] = None,
    fan_out_axes: Optional[Sequence[int]] = None
) -> Tuple[Optional[int], Optional[int]]:
  """Returns (fan_in, fan_out) of a weight variable of the given shape."""
  if not shape:
    return None, None
  if len(shape) < 1:
    return 1, 1
  elif len(shape) == 1:
    # Following _compute_fans() from TF's init_ops.py.
    return shape[0], shape[0]
  else:
    if fan_in_axes is None and fan_out_axes is None:
      fan_in_axes = [-2]
      fan_out_axes = [-1]
      receptive_field_axis = list(range(len(shape)))[:-2]
    else:
      assert fan_in_axes is not None
      assert fan_out_axes is not None
      receptive_field_axis = []

    receptive_field_size = 1
    for i in receptive_field_axis:
      receptive_field_size *= shape[i]
    fan_in = 1
    for i in fan_in_axes:
      fan_in *= shape[i]
    fan_in *= receptive_field_size
    fan_out = 1
    for i in fan_out_axes:
      fan_out *= shape[i]
    fan_out *= receptive_field_size
    return fan_in, fan_out


def scaled_orthogonal(key: JTensor,
                      shape: Sequence[int],
                      dtype: jnp.dtype = jnp.float32):
  """Scaled orthogonal initialization."""
  scale = max(np.sqrt(float(shape[-2]) / shape[-1]), 1)
  ortho_init = jax.nn.initializers.orthogonal(
      scale=scale, column_axis=-1, dtype=dtype)
  return ortho_init(key, shape)


def scaled_delta_orthogonal(key: JTensor,
                            shape: Sequence[int],
                            dtype: jnp.dtype = jnp.float32):
  """Delta orthogonal kernels; see arXiv:1806.05393 / arxiv:2110.01765."""
  if len(shape) not in [3, 4, 5]:
    raise ValueError(
        'Delta orthogonal initializer requires a 3D, 4D or 5D shape.')
  ortho_matrix = scaled_orthogonal(key, shape[-2:], dtype=dtype)
  w = jnp.zeros(shape, dtype=dtype)
  if len(shape) == 3:
    k = shape[0]
    return w.at[(k - 1) // 2, ...].set(ortho_matrix)
  elif len(shape) == 4:
    k1, k2 = shape[:2]
    return w.at[(k1 - 1) // 2, (k2 - 1) // 2, ...].set(ortho_matrix)
  else:
    k1, k2, k3 = shape[:3]
    return w.at[(k1 - 1) // 2, (k2 - 1) // 2, (k3 - 1) // 2,
                ...].set(ortho_matrix)


# Caller ensures that `prng_key` is different for different init_var calls.
def init_var(
    var_p: WeightHParams, prng_key: PRNGKey, var_full_name: str
) -> JTensor:
  """Creates an initial value of a var."""
  method = var_p.init.method
  scale = var_p.init.scale
  assert isinstance(scale, (int, float))
  shape = var_p.shape
  init_dtype = var_p.dtype
  fan_in_axes = var_p.fan_in_axes
  fan_out_axes = var_p.fan_out_axes
  logging.info(
      'Creating var %s with shape=%s, dtype=%s, init method=%s and scale=%s',
      var_full_name, shape, init_dtype.dtype, var_p.init.method,
      var_p.init.scale)
  # We rely on nn.scan to transform vars, hence init_var shouldn't expect a
  # repeat_prefix or repeat_prefix_split_dims_mapping.
  assert not var_p.repeat_prefix
  assert not var_p.repeat_prefix_split_dims_mapping

  if shape:
    assert all([dim_size > 0 for dim_size in shape
               ]), 'shape of %s is %s' % (var_full_name, shape)
    dim0 = shape[0]
  else:
    dim0 = 1

  if is_default_param_init(var_p.init):
    logging.debug(
            'WARNING!!! var %s is using the default xavier initializer.'
        ' Make sure this is intended.', var_full_name)

  if method in ['delta_orthogonal']:
    if len(shape) < 2:
      logging.warning('WARNING!! Delta orthogonal applied to 0/1D vars.')

  if (method in [
      'gaussian_sqrt_dim', 'uniform_sqrt_dim', 'truncated_gaussian_sqrt_dim'
  ]):
    if len(shape) > 2:
      # This is probably not the right method to use when len(shape) > 2,
      # e.g. dim0 will be 3 with a 3x3 conv2d kernel.
      logging.warning(
              'Initializing %s of shape %s with method %s: dim0=%s. '
          'Make sure that it is intended.', var_full_name, shape, method, dim0)
    scale *= 1.0 / math.sqrt(dim0)
  if method in ['gaussian_sqrt_fanin', 'truncated_gaussian_sqrt_fanin']:
    fan_in, _ = get_fan_in_fan_out(shape, fan_in_axes, fan_out_axes)
    if fan_in is not None:
      scale *= 1.0 / math.sqrt(fan_in)
  if method in ['gaussian_sqrt_fanout', 'truncated_gaussian_sqrt_fanout']:
    _, fan_out = get_fan_in_fan_out(shape, fan_in_axes, fan_out_axes)
    if fan_out is not None:
      scale *= 1.0 / math.sqrt(fan_out)
  if method in ['gaussian_sqrt_fanavg', 'uniform_sqrt_fanavg']:
    fan_in, fan_out = get_fan_in_fan_out(shape, fan_in_axes, fan_out_axes)
    if fan_in is not None and fan_out is not None:
      scale *= math.sqrt(2.0 / (fan_in + fan_out))

  if method in ['delta_orthogonal']:
    if len(shape) < 2:
      return scale * jrandom.normal(prng_key, shape, init_dtype)
    elif len(shape) == 2:
      return scaled_orthogonal(prng_key, shape, init_dtype)
    else:
      return scaled_delta_orthogonal(prng_key, shape, init_dtype)
  if method in [
      'gaussian', 'gaussian_sqrt_dim', 'gaussian_sqrt_fanin',
      'gaussian_sqrt_fanout', 'gaussian_sqrt_fanavg'
  ]:
    return scale * jrandom.normal(prng_key, shape, init_dtype)
  elif method in ['uniform', 'uniform_sqrt_dim']:
    return scale * jrandom.uniform(
        prng_key, shape, init_dtype, minval=-1.0, maxval=1.0)
  elif method in ['uniform_sqrt_fanavg']:
    return (
        jnp.sqrt(3)
        * scale
        * jrandom.uniform(prng_key, shape, init_dtype, minval=-1.0, maxval=1.0)
    )
  elif method in [
      'truncated_gaussian',
      'truncated_gaussian_sqrt_dim',
      'truncated_gaussian_sqrt_fanin',
      'truncated_gaussian_sqrt_fanout',
  ]:
    return scale * jrandom.truncated_normal(
        prng_key, lower=-2.0, upper=2.0, shape=shape, dtype=init_dtype)
  elif method in ['constant']:
    return scale + jnp.zeros(shape=shape, dtype=init_dtype)
  elif method in ['xavier']:
    fan_in, fan_out = get_fan_in_fan_out(shape, fan_in_axes, fan_out_axes)
    limit = scale * math.sqrt(6. / (fan_in + fan_out))
    return limit * jrandom.uniform(
        prng_key, shape, init_dtype, minval=-1.0, maxval=1.0)
  elif method in ['uniform_unit_scaling']:
    input_size = 1.0
    for dim in shape[:-1]:
      input_size *= float(dim)
    # Avoid errors when initializing zero-size tensors.
    input_size = max(input_size, 1.0)
    max_val = math.sqrt(3 / input_size) * scale
    return max_val * jrandom.uniform(
        prng_key, shape, init_dtype, minval=-1.0, maxval=1.0)
  else:
    assert False, 'init_type %s not supported.' % method


@struct.dataclass
class BoxedParam(struct.PyTreeNode, AxisMetadata):
  """Boxed param with WeightHParam metadata.

  BoxedParam allows us to treat the actual variable jnp.array and its
  associated metadata, i.e. WeightHParam as a single Flax variable collection.
  """

  # `value` is the jnp.array of the variable.
  value: Any
  # `meta` is the WeightParam declared for the variable.
  # We do not want to transform the variable weight param so we mark the field
  # pytree_node=False to prevent JAX transforms from touching it.
  meta: WeightHParams = struct.field(pytree_node=False)

  def __post_init__(self):
    assert not isinstance(self.value,
                          BoxedParam), 'Cannot double-box a parameter!'

  def unbox(self, apply_constraint=True) -> Any:
    # Do not locally apply pjit.with_sharding_constraint.
    del apply_constraint
    return self.value

  def replace_boxed(self, val: Any) -> TAxisMetadata:
    return self.replace(value=val)

  def add_axis(
      self, index: int, metadata_params: Dict[Any, Any]
  ) -> TAxisMetadata:
    if index != 0:
      raise ValueError('Only index==0 is implemented; given index=', index)
    if not metadata_params['is_initializing']:
      return self

    x_times = metadata_params['x_times']
    wp_sub = metadata_params['sub_weight_split_dims_mapping']
    optimizer_dims_mapping = metadata_params['optimizer_dims_mapping']

    if wp_sub is not None:
      assert isinstance(wp_sub, (list, tuple))
      assert len(wp_sub) == 1
      wp_sub = tuple(wp_sub)
    else:
      wp_sub = (-1,)

    if optimizer_dims_mapping is not None:
      assert isinstance(optimizer_dims_mapping, (list, tuple))
      assert len(optimizer_dims_mapping) == 1
      optimizer_dims_mapping = tuple(optimizer_dims_mapping)
    else:
      optimizer_dims_mapping = (-1,)

    if self.meta.repeat_prefix:
      assert isinstance(self.meta.repeat_prefix, list)
      repeat_prefix = [x_times] + self.meta.repeat_prefix
    else:
      repeat_prefix = [x_times]

    if self.meta.repeat_prefix_split_dims_mapping:
      assert isinstance(self.meta.repeat_prefix_split_dims_mapping, tuple)
      repeat_prefix_split_dims_mapping = wp_sub + tuple(
          self.meta.repeat_prefix_split_dims_mapping
      )
    else:
      repeat_prefix_split_dims_mapping = wp_sub

    if self.meta.repeat_optimizer_dims_mapping:
      assert isinstance(self.meta.repeat_optimizer_dims_mapping, tuple)
      repeat_optimizer_dims_mapping = optimizer_dims_mapping + tuple(
          self.meta.repeat_optimizer_dims_mapping
      )
    else:
      repeat_optimizer_dims_mapping = optimizer_dims_mapping

    new_meta = copy.deepcopy(self.meta)
    new_meta.repeat_prefix = repeat_prefix
    new_meta.repeat_prefix_split_dims_mapping = repeat_prefix_split_dims_mapping
    new_meta.repeat_optimizer_dims_mapping = repeat_optimizer_dims_mapping
    return self.replace(meta=new_meta)

  def remove_axis(
      self, index: int, metadata_params: Dict[Any, Any]
  ) -> TAxisMetadata:
    if index != 0:
      raise ValueError('Only index==0 is implemented; given index=', index)
    if not metadata_params['is_initializing']:
      return self

    x_times = metadata_params['x_times']
    wp_sub = metadata_params['sub_weight_split_dims_mapping']
    optimizer_dims_mapping = metadata_params['optimizer_dims_mapping']

    if wp_sub is not None:
      assert isinstance(wp_sub, (list, tuple))
      assert len(wp_sub) == 1
      wp_sub = tuple(wp_sub)
    else:
      wp_sub = (-1,)

    if optimizer_dims_mapping is not None:
      assert isinstance(optimizer_dims_mapping, (list, tuple))
      assert len(optimizer_dims_mapping) == 1
      optimizer_dims_mapping = tuple(optimizer_dims_mapping)
    else:
      optimizer_dims_mapping = (-1,)

    new_meta = copy.deepcopy(self.meta)
    if new_meta.repeat_prefix:
      assert isinstance(new_meta.repeat_prefix, list)
      removed_axis = new_meta.repeat_prefix.pop(0)
      assert removed_axis == x_times

    if new_meta.repeat_prefix_split_dims_mapping:
      assert isinstance(new_meta.repeat_prefix_split_dims_mapping, tuple)
      updated_dims_mapping = list(new_meta.repeat_prefix_split_dims_mapping)
      removed = updated_dims_mapping.pop(0)
      assert (removed,) == tuple(wp_sub)
      new_meta.repeat_prefix_split_dims_mapping = updated_dims_mapping

    if new_meta.repeat_optimizer_dims_mapping:
      assert isinstance(new_meta.repeat_optimizer_dims_mapping, tuple)
      updated_dims_mapping = list(new_meta.repeat_optimizer_dims_mapping)
      removed = updated_dims_mapping.pop(0)
      assert (removed,) == tuple(optimizer_dims_mapping)
      new_meta.repeat_optimizer_dims_mapping = updated_dims_mapping

    return self.replace(meta=new_meta)


@struct.dataclass
class WrappedHParams:
  # We do not want to transform hyper-params in post_init_hparams, so we mark
  # the field pytree_node=False to prevent JAX transforms from touching it.
  meta: Union[BaseHyperParams,
              pax_fiddle.Config] = struct.field(pytree_node=False)


@struct.dataclass
class AuxLossStruct:
  value: JTensor
  weight: JTensor


def maybe_unbox_value(tree):
  """Return the `value` leaf component of the pytree if it is a BoxedParam."""
  return jax.tree_map(
      lambda bp: bp.value if isinstance(bp, BoxedParam) else bp,
      tree,
      is_leaf=lambda x: isinstance(x, BoxedParam))


def unbox_meta(tree):
  """Return the `meta` leaf component of the pytree of BoxedParam."""
  return jax.tree_map(
      lambda bp: bp.meta, tree, is_leaf=lambda x: isinstance(x, BoxedParam))


class SummaryType(enum.Enum):
  """Types of summary tensors."""
  SCALAR = 1
  IMAGE = 2
  TEXT = 5
  AUDIO = 6
  # VIDEO summaries can be added to tensorboard using VideoSummaryMetric under
  # pax multimodal metrics but not add_summary.
  VIDEO = 7
  HISTOGRAM = 8

  # Like SCALAR, but this type indicates that this data is suitable for use
  # with sensitive data.
  AGGREGATE_SCALAR = 3

  # Like IMAGE, but this type indicates that the image data was sufficiently
  # aggregated such that this is safe to use with sensitive data.
  AGGREGATE_IMAGE = 4

  # TODO(nanxinchen): add AGGREGATE_AUDIO if needed


def get_summary_base_type(summary_type: SummaryType) -> SummaryType:
  if summary_type == SummaryType.AGGREGATE_SCALAR:
    return SummaryType.SCALAR
  if summary_type == SummaryType.AGGREGATE_IMAGE:
    return SummaryType.IMAGE
  return summary_type


def get_summary_type_suffix(summary_type: SummaryType) -> str:
  return '_' + get_summary_base_type(summary_type).name.lower()


def get_summary_type_from_key(key: str) -> SummaryType:
  for t in SummaryType:
    if key.endswith('_' + t.name.lower()):
      return t
  raise ValueError('Cannot parse summary type from key: ' + key)


def trim_summary_type_from_key(key: str) -> str:
  for t in SummaryType:
    suffix = '_' + t.name.lower()
    if key.endswith(suffix):
      return key[:-len(suffix)]
  raise ValueError('Cannot parse summary type from key: ' + key)


class _SummaryDict:
  """A dict holding summaries generated during forward computation.

  Currently it supports 8 types: SCALAR, AGGREGATE_SCALAR, IMAGE,
  AGGREGATE_IMAGE, TEXT, AUDIO, SUMMARY, HISTOGRAM. Keys will be appended with a
  type suffix.
  """

  def __init__(self) -> None:
    self.dict = {}

  def add_summary(self, name: str, tensor: JTensor,
                  summary_type: SummaryType) -> None:
    """Adds named summary to the thread local dict.

    Args:
      name: name of the summary.
      tensor: value of the summary.
      summary_type: type of the summary.
    """
    summary_base_name = name
    summary_suffix = get_summary_type_suffix(summary_type)
    full_name = summary_base_name + summary_suffix
    next_iter = 0
    while full_name in self.dict:
      next_iter += 1
      full_name = summary_base_name + str(next_iter) + summary_suffix
    if (summary_type == SummaryType.IMAGE
        or summary_type == SummaryType.AGGREGATE_IMAGE):
      if tensor.ndim == 3:
        # Add a batch dim.
        tensor = jnp.expand_dims(tensor, 0)
      assert tensor.ndim == 4
    if summary_type == SummaryType.AUDIO:
      if tensor.ndim == 2:
        # Add a batch dim.
        tensor = jnp.expand_dims(tensor, 0)
    self.dict[full_name] = tensor

  def clear(self) -> None:
    """Clears all summaries."""
    self.dict = {}


# A small structure that stores a shared layer and the hparams that were used in
# creating the layer. Note, hparams might be different from layer.hparam as
# during creation of layer, layer.hparams might have undergone modifications.
# We also keep a reference to the wrapper layer, to prevent it from being
# garbage-collected.
_SharedLayerCacheEntry = collections.namedtuple('_SharedLayerCacheEntry',
                                                ['layer', 'hparams', 'wrapper'])


class JaxContext:
  """Global context under which jax computations are carried out."""

  class HParams(base_hyperparams.BaseHyperParams):
    """HParams for `JaxContent`.

    Attributes:
      do_eval: Whether to do eval.
      summary_verbosity: int, defines the verbosity level for summaries context.
        The following are some notes on summary verbosity levels: * The larger
        the verbosity value, the more verbose. * The convention is to use
        non-negative integers. * The default verbosity level at the context
        level is 3, meaning that we'll log any summary written with verbosity <=
        3 by default. * Summaries are written if context_verbosity >=
        callsite_verbosity.
      mesh_axes_transpose: Optional axes transpose rules for the device mesh. It
        is a dict of {new_axis_name: old_axis_name}. Within this context,
        new_axis_name in all shardings will be translated to old_axis_name in
        the original device mesh.
    """
    do_eval: Optional[bool] = None
    summary_verbosity: int = 3
    mesh_axes_transpose: Optional[Dict[str, str]] = None

  def __init__(self, hparams: JaxContext.HParams) -> None:
    self._hparams = hparams.clone()
    self._summary_dict = _SummaryDict()
    # This is a dict of dict. The inner dict is a map from string to layer
    # object, and the outer dict is keyed by the root scope. The intended Use
    # of this is for passing shared layers. A special shared_layer_id identifier
    # is used to indicate that multiple modules should share the same underlying
    # model weights.
    self._root_scope_to_shared_layers_map: Dict[Any, Dict[
        str, _SharedLayerCacheEntry]] = (
            collections.defaultdict(
                lambda: collections.defaultdict(lambda: None)))

  @property
  def summary_dict(self) -> _SummaryDict:
    return self._summary_dict

  @property
  def hparams(self) -> JaxContext.HParams:
    return self._hparams

  @property
  def do_eval(self) -> bool:
    return self.hparams.do_eval

  @property
  def summary_verbosity(self) -> int:
    return self.hparams.summary_verbosity

  def __enter__(self) -> JaxContext:
    _JaxContextStack.stack.append(self)
    return self

  def __exit__(self, type_arg, value_arg, traceback_arg):
    assert _JaxContextStack.stack
    assert _JaxContextStack.stack[-1] is self
    _JaxContextStack.stack.pop()

  @staticmethod
  def top() -> Optional[JaxContext]:
    return _JaxContextStack.stack[-1] if _JaxContextStack.stack else None

  @staticmethod
  def has_context() -> bool:
    """Whether there currently exists a global jax context."""
    return len(_JaxContextStack.stack) > 0  # pylint: disable=g-explicit-length-test

  @staticmethod
  def new_context(*,
                  hparams: Optional[JaxContext.HParams] = None) -> JaxContext:
    """Returns a new empty JaxContext.

    Args:
      hparams: if not None, an instance of JaxContext.HParams(). If it is None,
        the newly constructed JaxContext will assume the same params as the
        current context if it is not None, or the default one.

    Returns:
      A new JaxContext.
    """
    if hparams is None:
      current = JaxContext.top()
      if current is None:
        new_hparams = JaxContext.HParams()
      else:
        new_hparams = current.hparams.clone()
    else:
      new_hparams = hparams.clone()
    context = JaxContext(new_hparams)
    return context

  def lookup_shared_layer(
      self, root_scope: flax_core.Scope,
      shared_layer_id: str) -> Optional[_SharedLayerCacheEntry]:
    logging.info('lookup_shared_layer called with id: %s in the scope of %s',
                 shared_layer_id, root_scope)
    return self._root_scope_to_shared_layers_map[root_scope][shared_layer_id]

  def set_shared_layer(self, root_scope: flax_core.Scope, shared_layer_id: str,
                       wrapper: _WrapperLayer, layer_hparams):
    logging.info('set_shared_layer called with id: %s in the scope of %s',
                 shared_layer_id, root_scope)
    existing = self.lookup_shared_layer(root_scope, shared_layer_id)
    assert existing is None
    self._root_scope_to_shared_layers_map[root_scope][
        shared_layer_id] = _SharedLayerCacheEntry(
            layer=wrapper.cld, hparams=layer_hparams.clone(), wrapper=wrapper)


def cur_jax_context() -> JaxContext:
  current = JaxContext.top()
  assert current is not None
  return current


def add_global_summary(
    name: str,
    tensor: JTensor,
    summary_type: SummaryType = SummaryType.SCALAR,
    verbosity: int = 2) -> None:
  """Adds a global summary tensor.

  This summary is not associated with any particular layer and is added to the
  global JaxContext collection.

  Args:
    name: name of the summary.
    tensor: value of the summary.
    summary_type: type of the summary. Currently it supports 3 types: SCALAR,
      IMAGE, AUDIO, HISTOGRAM. Keys will be appended with a type suffix. Image
      tensors must be either [batch, height, width, channels] or [height, width,
      channels]. The histograms are computed over the batch and do not have the
      batch dimension.
    verbosity: verbosity level for the summary to add. If the current jax
      context's verbosity level is less verbose (lower value) than the summary,
      the summary does not get added. Refer to
      JaxContext.HParams.summary_verbosity docstring for more detail.
  """
  context = cur_jax_context()
  if verbosity > context.summary_verbosity:
    return

  global_namespace_name = name
  if jnp.issubdtype(tensor.dtype, jnp.floating):
    tensor = tensor.astype(jnp.float32)
  context.summary_dict.add_summary(global_namespace_name, tensor, summary_type)


def clear_global_summary() -> None:
  context = cur_jax_context()
  context.summary_dict.clear()


def all_global_summaries() -> SummaryDict:
  context = cur_jax_context()
  return context.summary_dict.dict


class Theta:
  """Dot syntax accession helper to be used inside a descriptor."""

  def __init__(self, module):
    # module is a BaseLayer instance.
    self.module = module

  def __getattr__(self, k):
    self.module._try_setup()
    if not self.module.has_variable('params', k):
      raise ValueError(f'Module {self.module} has no theta.{k} defined.')
    # Cast BaseLayer.theta to fprop_dtype to ensure BaseLayer.init respects
    # fprop_dtype.
    variable = self.module.get_variable('params', k)
    var_hparams = self.module._weight_hparams[k]
    if (self.module.fprop_dtype == jnp.bfloat16 and
        var_disallow_bfloat16_conversion(var_hparams)):
      return variable
    return self.module._cast_to_fprop_dtype(variable)

  def __getitem__(self, k):
    self.module._try_setup()
    if not self.module.has_variable('params', k):
      raise ValueError(f'Module {self.module} has no theta[{k}] defined.')
    # Cast BaseLayer.theta to fprop_dtype to ensure BaseLayer.init respects
    # fprop_dtype.
    variable = self.module.get_variable('params', k)
    var_hparams = self.module._weight_hparams[k]
    if (self.module.fprop_dtype == jnp.bfloat16 and
        var_disallow_bfloat16_conversion(var_hparams)):
      return variable
    return self.module._cast_to_fprop_dtype(variable)


class ThetaDescriptor:
  """Dot syntax accession descriptor."""

  def __get__(self, obj, objtype=None):
    return Theta(obj)


_BaseLayerRecursionDictKeysToIgnore = [
    'parent', '_theta', '_weight_hparams', '_state', 'scope',
    '_private_hparams', 'hparams', '_private_children'
]


def instantiate_layer(layer_p: pax_fiddle.Config, scope: Any) -> BaseLayer:
  """Instantiates a layer parameterized with layer_p.

  If a layer with the same shared_name exists under the scope 'scope', the
  cached layer will be returned.

  Args:
    layer_p: `HParams` or `fdl.Config` for the layer to be instantiated.
    scope: The scope under which the layer to be created.

  Returns:
    A newly created layer or a cached layer if desired.
  """
  if layer_p.shared_weight_layer_id:
    jax_context = cur_jax_context()
    assert jax_context is not None
    pre_created = jax_context.lookup_shared_layer(
        scope, layer_p.shared_weight_layer_id)
    if pre_created is not None:
      assert compatible_hparams(
          pre_created.hparams,
          layer_p), (f'shared layers are of incompatible configs '
          f'\n\n{pre_created.hparams.to_text()} \n\n vs '
                     f'\n\n {layer_p.to_text()}')
      # simply reuse existing layer.
      layer = pre_created.layer
    else:
      # Always place the shared layers under the scope, under the unique
      # namespace p.name. This makes sure that the variables of the shared
      # layer is uniquely addressable regardless of the order they are
      # created.
      wrapped_p = layer_p.clone()
      wrapped_p.shared_weight_layer_id = None
      wrapper_p = pax_fiddle.Config(
          _WrapperLayer, name=layer_p.shared_weight_layer_id, cld_tpl=wrapped_p
      )
      wrapper = instantiate(wrapper_p, parent=scope)
      layer = wrapper.cld
      jax_context.set_shared_layer(scope, layer_p.shared_weight_layer_id,
                                   wrapper, layer_p.clone())
  else:
    # simply create the child
    layer = layer_p.Instantiate()
  return layer


def _maybe_to_bfloat16_dtype(x):
  """Maybe convert input to bf16 dtype.

  Args:
    x: common array types like JTensor or ShapeDtypeStruct.

  Returns:
    A casted ShapeDtypeStruct if x is one of JTensor or ShapeDtypeStruct.
    Otherwise, returns x.
  """
  if not hasattr(x, 'dtype'):
    # Ignore common non-array types that shouldn't be cast.
    return x
  elif x.dtype in [jnp.float32, np.float32]:
    return jax.ShapeDtypeStruct(x.shape, jnp.bfloat16)
  else:
    return jax.ShapeDtypeStruct(x.shape, x.dtype)


# BoxedPartitionSpec holds the PartitionSpec in 'meta'.
@struct.dataclass
class BoxedPartitionSpec:
  meta: Any = struct.field(pytree_node=False)


def _weight_hparam_to_pspec(hparam, mesh_axis_names) -> BoxedPartitionSpec:
  """Converts split_dims_mapping weight hparam to BoxedPartitionSpec.

  Args:
    hparam: the weight hparam to be converted.
    mesh_axis_names: A tuple/list of strings of the name of the device mesh.

  Returns:
    A BoxedPartitionSpec.
  """
  mapping = hparam.tensor_split_dims_mapping
  if mapping is None:
    mapping = [None] * len(hparam.shape)
  return BoxedPartitionSpec(meta=to_partition_spec(mapping, mesh_axis_names))


@dataclasses.dataclass(frozen=True)
class _FiddleHParamsClassStub(
    type, base_hyperparams.OverrideSubConfigFieldProtocol
):
  """Backwards-compatibility stub for `HParams` attribute in `BaseLayer`.

  Can be used with base_hyperparams.sub_config_field.  E.g.:

    >>> class MyLayer(base_layer.BaseLayer):
    ...   bias_tpl: LayerTpl = sub_config_field(Bias.HParams)

  Can be called to generate a `fdl.Config` for `fiddle_base_layer_cls`.  E.g.:

    >>> bias_tpl = Bias.HParams(
    ...     name='bias', dims=p.output_dims, bias_init=p.bias_init)

  Can be used as type argument to `isinstance` -- returns true if the instance
  is a `pax_fiddle.Config` whose `__fn_or_cls__` is `fiddle_base_layer_cls`:

    >>> isinstance(pax_fiddle.Config(MyLayer), MyLayer.HParams)
    True

  TODO(b/249483164): Remove this stub once the HParams->Fiddle migration is
  complete.
  """

  fiddle_base_layer_cls: Type[BaseLayer]

  def __new__(cls, fiddle_base_layer_cls, *args):
    if args:
      # The code in this block ensures that if the user does:
      #   class Parent(BaseLayer):
      #     x: int = 0
      #   class Child(Parent):
      #     class HParams(Parent.HParams):  # [*]
      #       y: int = 0
      # then an exception will be raised on the line marked with [*].
      bases, cls_dict = args  # pylint: disable=unused-variable
      assert len(bases) == 1, 'Expected HParams to have a single base'
      base_cls = bases[0].fiddle_base_layer_cls
      raise ValueError(
          f'For {base_cls}: PAX layers should no longer use nested HParams '
          'classes. Instead, add fields directly to the layer class.'
      )
    name = 'HParams'
    qualname = f'{fiddle_base_layer_cls.__qualname__}.{name}'
    namespace = {
        '__qualname__': qualname,
        'fiddle_base_layer_cls': fiddle_base_layer_cls,
    }
    bases = ()
    # pylint: disable=unused-variable
    return super().__new__(cls, name, bases, namespace)  # pytype: disable=wrong-arg-count

  def __init__(cls, fiddle_base_layer_cls):
    pass

  def __instancecheck__(cls, instance):
    return (
        isinstance(instance, pax_fiddle.Config)
        and isinstance(fdl.get_callable(instance), type)
        and issubclass(fdl.get_callable(instance), cls.fiddle_base_layer_cls)
    )

  def __to_sub_config_field__(cls):
    return template_field(cls.fiddle_base_layer_cls)

  def __call__(cls, *args, **kwargs):
    return pax_fiddle.Config(cls.fiddle_base_layer_cls, *args, **kwargs)

  def config(cls, **kwargs):
    return cls(**kwargs)


class BaseLayer(nn.Module):
  """Base class for layers that are configured using Fiddle.

  Subclasses are expected to:

  * Declare any configuration parameters using dataclass field syntax.
  * Define a setup() method, which creates sub-layers and layer variables.
  * Define a __call__() method, which carries out the ML computation.

  TODO(pax-team): Add more doc-string and example.

  Attributes:
    dtype: Default dtype for all variables.
    fprop_dtype: Activations datatype to use.
    params_init: How model weights should be initialized.
    skip_lp_regularization:  If True, all variables in this layer will skip Lp
      regularization. If None/False, only variables explicitly in the
      SKIP_LP_REGULARIZATION collection will skip Lp regularization. Also
      propagated to child layers with default settings (None).
    ici_mesh_shape: Shape of the logical mesh used for SPMD parallelism in each
      slice. The meaning of each mesh axis is defined by mesh_axis_names, so
      these two params must be the same length. If dcn_mesh_shape is present,
      the overall mesh is the product of ici_mesh_shape and dcn_mesh_shape. For
      example, an ici_mesh_shape of [2, 3, 4] with mesh_axis_names ['replica',
      'data', 'mdl'] indicates 2-way replica parallelism, 3-way data
      parallelism, and 4-way model parallelism over 24 devices. None, the
      default, is equivalent to a sequence of ones and means that the model is
      placed on a single device.
    dcn_mesh_shape: Shape of the logical mesh used for SPMD parallelism over
      multiple slices. The overall mesh is the product of ici_mesh_shape and
      dcn_mesh_shape, and the meaning of each mesh axis is defined by
      mesh_axis_names, so these three params must be the same length. For
      example, a dcn_mesh_shape of [2, 2, 1, 1] with mesh_axis_names ['stage',
      'replica', 'data', 'mdl'] indicates 2-way pipeline parallelism and 2-way
      replica parallelism over 4 slices. None, the default, is equivalent to a
      sequence of ones and means that the model is placed on a single slice.
    contiguous_submeshes: If True, this will be passed to the
      mesh_utils.create_device_mesh() call and it will attempt to create a mesh
      where each process's local devices form a contiguous submesh. This is
      unused when `dcn_mesh_shape` is not None.
    mesh_axis_names: Names for each mesh axis in ici_mesh_shape and/or
      dcn_mesh_shape. Common mesh axes include 'replica' for replica
      parallelism, 'data' for data parallelism, 'mdl' for model parallelism, and
      'stage' for pipeline parallelism.
    weight_split_dims_mapping: Relevant only if the mesh shape params above are
      not None. It specifies how weight of this layer or those of the sublayers
      should be sharded over the overall device mesh. This field will be
      dynamically bound to WeightSharding dataclass above.
    activation_split_dims_mapping: Relevant only if the mesh shape params above
      are not None. It specifies how activation of this layer or those of the
      sublayers should be sharded over the overall device mesh. This field will
      be dynamically bound to the ActivationSharding dataclass above.
    shared_weight_layer_id: a unique id indicating weight sharing. Layers with
      the same 'shared_weight_layer_id' share the same underlying model weights.
  """

  @dataclasses.dataclass(frozen=True)
  class WeightSharding(pax_fiddle.CloneAndSetMixin):
    """Represents how layer's learned parameters are partitioned across a mesh.

    This usually refers to the primary model weight. Sub-layers can define
    additional params for more weights.

    Attributes:
      wt: Sharding annotations for the primary model weight.
    """

    wt: SplitDimsMapping = None

  @dataclasses.dataclass(frozen=True)
  class ActivationSharding(pax_fiddle.CloneAndSetMixin):
    """Represents how intermediate values should be partitioned across a mesh.

    This usually refers to the primary layer output. Sub-layers can define
    additional params for more activations.

    Attributes:
      out: Sharding annotations for the primary layer output.
    """

    out: SplitDimsMapping = None

  dtype: jnp.dtype = jnp.float32
  fprop_dtype: Optional[Any] = None
  params_init: WeightInit = instance_field(default_param_init)
  skip_lp_regularization: Optional[bool] = None
  ici_mesh_shape: Optional[Sequence[int]] = None
  dcn_mesh_shape: Optional[Sequence[int]] = None
  contiguous_submeshes: bool = False
  mesh_axis_names: Optional[Sequence[str]] = None
  shared_weight_layer_id: Optional[str] = None
  # TODO(b/249483164): Change these to use instance_field rather than
  # template_field after the Fiddle migration.
  weight_split_dims_mapping: pax_fiddle.Config[BaseLayer.WeightSharding] = template_field(
      WeightSharding
  )
  activation_split_dims_mapping: pax_fiddle.Config[
      BaseLayer.ActivationSharding
  ] = template_field(ActivationSharding)

  @property
  def mesh_shape(self):
    if self.ici_mesh_shape is not None:
      assert len(self.ici_mesh_shape) == len(self.mesh_axis_names)
    if self.dcn_mesh_shape is None:
      return self.ici_mesh_shape
    else:
      assert len(self.ici_mesh_shape) == len(self.dcn_mesh_shape)
      return [i * d for i, d in zip(self.ici_mesh_shape, self.dcn_mesh_shape)]

  # Fetches variables from flax 'params' class via theta "dot" syntax.
  theta = ThetaDescriptor()

  def _to_fdl_config(self) -> pax_fiddle.Config[BaseLayer]:
    """Returns a `fdl.Config` template for this BaseLayer."""
    kwargs = {}
    for field in dataclasses.fields(self):
      if field.name == 'parent' or not field.init:
        continue
      value = getattr(self, field.name)
      if isinstance(value, BaseLayer):
        value = value.hparams
      kwargs[field.name] = value
    return pax_fiddle.Config(type(self), **kwargs)

  # Compatibility stub:
  # `self.hparams` returns a Fiddle Config that can be used to build self.
  hparams = functools.cached_property(_to_fdl_config)

  @staticmethod
  def copy_base_hparams(
      source: Union[pax_fiddle.Config, BaseLayer],
      target: pax_fiddle.Config,
  ):
    """Copies BaseLayer configuration parameters from `source` to `target`.

    This is used by `self.create_child` to allow child layers to "inherit" these
    parameters from their parent layer (unless they override them).  The
    following parameters are inherited: dtype, fprop_dtype,
    skip_lp_regularization, ici_mesh_shape, dcn_mesh_shape, and params_init.

    Args:
      source: The configuration object to copy parameters from.
      target: The configuration object to copy parameters to.  Mutated in-place.
    """
    assert isinstance(
        source, (pax_fiddle.Config, BaseLayer)
    ), source
    assert isinstance(target, pax_fiddle.Config), target
    if isinstance(source, pax_fiddle.Config):
      assert issubclass(fdl.get_callable(source), BaseLayer)
    assert issubclass(target.cls, BaseLayer)
    BaseLayer._copy_base_params_to_fdl_config(source, target)

  @staticmethod
  def _copy_base_hparams(
      source: Union[pax_fiddle.Config, BaseLayer], target: pax_fiddle.Config
  ):
    if target.dtype == jnp.float32:
      target.dtype = source.dtype
    if target.fprop_dtype is None:
      target.fprop_dtype = source.fprop_dtype
    if target.skip_lp_regularization is None:
      target.skip_lp_regularization = source.skip_lp_regularization
    if target.ici_mesh_shape is None:
      target.ici_mesh_shape = copy.deepcopy(source.ici_mesh_shape)
    if target.dcn_mesh_shape is None:
      target.dcn_mesh_shape = copy.deepcopy(source.dcn_mesh_shape)
    if target.mesh_axis_names is None:
      target.mesh_axis_names = copy.deepcopy(source.mesh_axis_names)
    if is_default_param_init(target.params_init):
      # Copy params_init as well. Both target.params_init and
      # source.params_init are hyperparams.HParams.
      # The only exception is when layer.setup override params_init with
      # Params().Set syntax in which case, source.params_init is a
      # WeightInit, copy.deepcopy(source.params_init) works in both cases.
      target.params_init = copy.deepcopy(source.params_init)

  @staticmethod
  def _copy_base_params_to_fdl_config(
      source: Union[pax_fiddle.Config, BaseLayer], target: pax_fiddle.Config
  ):
    # TODO(edloper): Once we start using `base_layer.instance_field`, we will
    # also need to copy base hparams to child objects in `pax_fiddle.build`
    # (because create_child won't get called for those sub-fields).

    # We copy from parent to child, then from child to grandchild, etc.  This
    # stack keeps track of the ancestors of `value` in `visit` (defined below).
    source_stack = [source]

    def visit(value, state: daglish.State) -> None:
      # Copy params if `value` is a BaseLayer config.
      value_is_base_layer = isinstance(value, BaseLayer) or (
          isinstance(value, pax_fiddle.Config)
          and isinstance(fdl.get_callable(value), type)
          and issubclass(fdl.get_callable(value), BaseLayer)
      )
      if value_is_base_layer:
        BaseLayer._copy_base_hparams(source_stack[-1], value)
        source_stack.append(value)

      # Recurse to child objects (skipping fields tagged "DoNotBuild").
      # We skip DoNotBuild objects, because those are child-templates, and
      # they will inherit parameters from their parent object when their
      # parent calls self.create_child.
      if isinstance(value, pax_fiddle.Config):
        for arg_name, arg_val in value.__arguments__.items():
          arg_tags = value.__argument_tags__.get(arg_name, ())
          if pax_fiddle.DoNotBuild not in arg_tags:
            state.call(arg_val, daglish.Attr(arg_name))
      elif state.is_traversable(value):
        state.flattened_map_children(value)

      if value_is_base_layer:
        source_stack.pop()

    daglish.MemoizedTraversal.run(visit, target)

  def post_init_hparams(self, *args):
    """Recursively populates the HYPER_PARAMS collection with hyper-params ...

    of self and all its children.

    The difference from self.hparams is that params here are post initialization
    tweaks and reflect the actual sub-layers being created.

    Args:
      *args: used for scan's rigid signature requirements.
    """

    def is_sublayer_template(val):
      template_types = (
          base_hyperparams.InstantiableHyperParams,
          pax_fiddle.Config,
      )
      if isinstance(val, template_types) and issubclass(val.cls, BaseLayer):
        return True

      # Check if val is a container of sub-layer templates.
      if isinstance(val, Mapping) and all(isinstance(key, str) for key in val):
        return is_sublayer_template(list(val.values()))
      if isinstance(val, (list, tuple)):
        if any(is_sublayer_template(child) for child in val):
          if all(is_sublayer_template(child) or child is None for child in val):
            return True
      return False

    hparam_kwargs = {}
    for field in self._hparam_fields:
      value = getattr(self, field)
      if is_sublayer_template(value) or isinstance(value, BaseLayer):
        # No need to include sub-layer template params (or direct-instantiated
        # children), since the instantiated sub-layer will show up in its own
        # collection anyways.  Use an explcit `None` value to prevent `fiddle`
        # from auto-populating fields with default factories.
        value = None
      hparam_kwargs[field] = value
    hparams = pax_fiddle.Config(type(self), **hparam_kwargs)

    self.put_variable(HYPER_PARAMS, '_hparams', WrappedHParams(hparams))
    # walk through all the attributes on self and recursively apply
    # post_init_hparams on submodules:
    for key, val in self.__dict__.items():
      if key in _BaseLayerRecursionDictKeysToIgnore:
        continue  # don't create recursion loop!

      def force(v):
        if isinstance(v, BaseLayer):
          # pass dummy args through - again only needed for scan.
          v.post_init_hparams(*args)

      jax.tree_map(force, val)
    return None

  @functools.cached_property
  def _hparam_fields(self) -> Set[str]:
    """Returns a list of hyperparameter field names for `self`."""
    return set(field.name for field in dataclasses.fields(self)
               if field.init and field.name != 'parent')

  @classmethod
  def __init_subclass__(cls, **kwargs: Any):
    cls._override_split_dim_mapping_fields()
    if 'HParams' in cls.__dict__:
      raise ValueError(
          f'For {cls}: PAX layers should no longer use nested HParams '
          'classes. Instead, add fields directly to the layer class.'
      )
    super().__init_subclass__(**kwargs)
    for field in dataclasses.fields(cls):
      if isinstance(field.default, fdl.Buildable):
        raise ValueError(
            f"{cls.__qualname__}.{field.name}'s default value is a mutable "
            'instance of fdl.Buildable.  Please update this field to use a '
            'default_factory instead, to avoid unintentional object sharing.'
        )
      if _is_template_type(field.type) and not pax_fiddle.has_do_not_build_tag(
          field
      ):
        raise ValueError(
            f'{cls.__qualname__}.{field.name} has a template type, but '
            'does not have the pax_fiddle.DoNotBuild tag set.  Please use '
            'base_layer.template_field to declare this field.')

  @classmethod
  def _override_split_dim_mapping_fields(cls):
    """Overrides the `*_split_dims_mapping` fields, if necessary.

    If WeightSharding or ActivationSharding were overridden by
    `cls`, then automatically transform them to a dataclass, and update the
    corresponding dataclass fields to use the new type for their
    default_factory.
    """
    if '__annotations__' not in cls.__dict__:
      cls.__annotations__ = {}
    if 'WeightSharding' in cls.__dict__:
      if not issubclass(cls.WeightSharding, BaseLayer.WeightSharding):
        raise ValueError(
            f'Expected {cls}.WeightSharding to be a subclass of '
            'BaseLayer.WeightSharding'
        )
      if not typing.TYPE_CHECKING:
        dataclasses.dataclass(frozen=True)(cls.WeightSharding)
      # TODO(b/249483164): Change this to use instance_field rather than
      # template_field after the Fiddle migration.
      cls.__annotations__['weight_split_dims_mapping'] = pax_fiddle.Config[
          cls.WeightSharding
      ]
      cls.weight_split_dims_mapping = template_field(cls.WeightSharding)
    if 'ActivationSharding' in cls.__dict__:
      if not issubclass(cls.ActivationSharding, BaseLayer.ActivationSharding):
        raise ValueError(
            f'Expected {cls}.ActivationSharding to be a subclass of '
            'BaseLayer.ActivationSharding'
        )
      if not typing.TYPE_CHECKING:
        dataclasses.dataclass(frozen=True)(cls.ActivationSharding)
      # TODO(b/249483164): Change this to use instance_field rather than
      # template_field after the Fiddle migration.
      cls.__annotations__['activation_split_dims_mapping'] = pax_fiddle.Config[
          cls.ActivationSharding
      ]
      cls.activation_split_dims_mapping = template_field(cls.ActivationSharding)

  def __post_init__(self):
    if isinstance(self.dtype, (BaseHyperParams, fdl.Config)):
      type_name = f'{type(self).__module__}.{type(self).__qualname__}'
      raise TypeError(
          f'Expected first argument to {type_name} to be a dtype, '
          f'but got a {type(self.dtype)} instead.  This can happen if '
          f'you try to instantiate {type_name} using '
          f'`{type_name}(layer_p)`, which is no longer supported for '
          'Fiddle-configured layers.  Please use `layer_p.Instantiate()` '
          'instead.'
      )
    # Note: we need to set fprop_dtype before we call super().__post_init__(),
    # because super().__post_init__() can mark `self` as frozen in some
    # contexts.
    if self.fprop_dtype is None:
      self.fprop_dtype = self.dtype
    object.__setattr__(self, '_theta', set())
    object.__setattr__(self, '_weight_hparams', {})
    object.__setattr__(self, '_private_children', {})
    super().__post_init__()

  @nn.nowrap
  def _try_setup(self, shallow=False):
    setup_status_before = self._state.setup_called
    super()._try_setup(shallow=shallow)
    setup_status_after = self._state.setup_called
    if setup_status_before != setup_status_after:
      # setup() is being called. Let's perform some sanity checks.
      for k, v in self._state.children.items():
        if v == 'param':
          assert k in self._theta, (
              f'Learnable param {k} is not created via create_variable helper.')
        else:
          pass

  # Similar to Flax nn.Module.init, except that BaseLayer param and variables
  # are created with WeightHParams as their metadata (e.g. SPMD annotations).
  # We store the variables with their metadata as a BoxedParams object inside
  # Flax variable collections. This wrapped BaseLayer.init tries to preserve
  # the same semantics as nn.Module.init by returning callers unboxed variables.
  #
  # This top level `init` call relies on the side effects of
  # self.param/self.variable for populating self.scope with the variable
  # collections. What gets put into self.scope is BoxedParams and thus the need
  # for unboxing for super().init().
  #
  # We intentionally unbox BoxedParams and only return jnp.array values to
  # callers because we expect users to do
  #   initial_vars = layer.init(k)
  #   outputs = layer.apply(initial_vars, method=layer.__call__)
  # where `initial_vars` that users see are unboxed jnp.arrays, and
  # also the code in __call__ never sees BoxedParams but always jnp.arrays.
  def init(self, rngs, *args, method=None, mutable=None, **kwargs):
    if method is None:
      method = self.__call__
    # Only allow PARAMS and NON_TRAINABLE to be mutable because the trainer
    # typically checkpoints only PARAMS and NON_TRAINABLE. The other variable
    # collections are typically associated with a single step only.
    mutable_list = DEFAULT_INIT_MUTABLE_LIST
    if mutable:
      # Allow users to override and ask for DECODE_CACHE init for example.
      mutable_list = mutable
    variables = super().init(
        rngs, *args, method=method, mutable=mutable_list, **kwargs)
    return flax_core.unfreeze(maybe_unbox_value(variables))

  # See comments in `init` above.
  # `variables` are unboxed variables, and not BoxedParams.
  def apply(self,
            variables,
            *args,
            rngs=None,
            method=None,
            mutable=False,
            capture_intermediates=False,
            **kwargs):
    # Default to self.__call__ to make callsite cleaner.
    if method is None:
      method = self.__call__
    result = super().apply(
        variables,
        *args,
        rngs=rngs,
        method=method,
        mutable=mutable,
        capture_intermediates=capture_intermediates,
        **kwargs)
    if mutable:
      outputs, updated_variables = result
      return outputs, flax_core.unfreeze(updated_variables)
    else:
      return result

  # In its essence, this is jax.eval_shape(model.init) except that we return
  # the WeightHParams objects to callers. This is typically used to retrieve
  # the unpadded variable shapes and SPMD annotations for
  # PARAMS and NON_TRAINABLE collections.
  def abstract_init_with_metadata(self,
                                  *args,
                                  do_eval=False,
                                  method=None,
                                  **kwargs) -> NestedWeightHParams:
    # Dummy key is enough because we eval_shape only.
    k = jax.random.PRNGKey(1)
    rngs = {PARAMS: k, RANDOM: k, NON_PAX_RNG_KEY: k}
    # Only PARAMS and NON_TRAINABLE have BoxedParam.
    init_fn = functools.partial(super().init,
                                mutable=DEFAULT_INIT_MUTABLE_LIST,
                                method=method)
    # Disable logging to reduce logspam.
    with py_utils.logging_verbosity_level('FATAL'):
      context_p = JaxContext.HParams(do_eval=do_eval)
      with JaxContext.new_context(hparams=context_p):
        if self.fprop_dtype == jnp.bfloat16:
          converted_args = jax.tree_map(_maybe_to_bfloat16_dtype, args)
          converted_kwargs = jax.tree_map(_maybe_to_bfloat16_dtype, kwargs)
        else:
          converted_args = args
          converted_kwargs = kwargs
        variables_abstract = jax.eval_shape(
            init_fn, rngs, *converted_args, **converted_kwargs)
    # If model contains FlaxAdapter, we may see 'params_axes' collections, but
    # they do not contain WeightHParams, so we remove them from returned values.
    if 'params_axes' in variables_abstract:
      del variables_abstract['params_axes']
    return flax_core.unfreeze(unbox_meta(variables_abstract))

  # Notes on Flax interoperability:
  #
  # If a Flax module contains a BaseLayer submodule, say
  #
  #   class FlaxModule(nn.Module):
  #     base_layer: BaseLayer
  #
  #     @@nn.compact
  #     def __call__(self, x):
  #       y = base_layer(x)
  #       ...
  #
  #  == How BaseLayer puts variables inside self.scope:
  #
  #  `FlaxModule.init` or `FlaxModule.apply` first call `FlaxModule.setup`,
  #  which then calls `base_layer.setup()` because `base_layer` is a submodule.
  #  `base_layer.setup()` calls `self.create_variable` which ultimately calls
  #  `self.param` and `self.variable` and put BoxedParams into self.scope.
  #
  #  == How FlaxModule.__call__ retrieves variables from self.scope
  #
  # `base_layer.__call__` internally may call `base_layer.theta` or
  # `base_layer.get_var` which ultimately calls `self.get_variable` to retrieve
  # the variables.
  # Since these variables are BoxedParams inside self.scope and FlaxModule
  # cannot handle BoxedParams, we need to unbox the return value of
  # `self.get_variable`.
  #
  # For Flax interop only, BaseLayer users should not use self.get_variable
  # directly. Note that this optional unboxing is a no-op for pure Pax case:
  #   initial_vars = layer.init(k)
  #   outputs = layer.apply(initial_vars, method=layer.__call__)
  # Here the unboxing is done at the top level `layer.init`, so self.scope
  # inside `layer.apply` sees unboxed variables, which means `self.get_variable`
  # also only sees already unboxed variables.
  def get_variable(self, col: str, name: str, default=None):
    retval = super().get_variable(col, name, default)
    # Unbox returned value in case Flax calls Pax.
    return maybe_unbox_value(retval)

  @nn.nowrap
  def add_summary(self,
                  name: str,
                  tensor: Union[JTensor, Callable[[], JTensor]],
                  summary_type: SummaryType = SummaryType.SCALAR,
                  verbosity: int = 2) -> None:
    """Add a tensor to the SUMMARIES collection.

    Args:
      name: name of the summary to be collected.
      tensor: the tensor containing the value to be written or a
        callable to evaluate to determine the value to be written.  A callable
        can be used in situations where early evaluation of the tensor might
        break specialized evaluation, e.g. jax2tf.
      summary_type: enum value indicating what type of summary is being added.
      verbosity: verbosity level of the summary being written. If this verbosity
        value is higher (less verbose) than that of the JaxContext the summary
        will not be added. If the summary is being without any JaxContext, it'll
        be added by default. Refer to JaxContext.HParams.summary_verbosity
        docstring for more detail.
    """
    # if not running under any jax context add summary by default
    if (JaxContext.has_context() and
        verbosity > self.jax_context.summary_verbosity):
      return

    if isinstance(tensor, Callable):
      tensor = tensor()

    next_iter = 0
    summary_name = name
    full_name = summary_name
    while self.has_variable(SUMMARIES, full_name):
      next_iter += 1
      full_name = summary_name + str(next_iter)
    full_name = full_name + get_summary_type_suffix(summary_type)
    if summary_type == SummaryType.IMAGE:
      if tensor.ndim == 3:
        # Add a batch dim.
        tensor = jnp.expand_dims(tensor, 0)
      assert tensor.ndim == 4
    elif summary_type == SummaryType.AUDIO:
      if tensor.ndim == 2:
        # Add a batch dim.
        tensor = jnp.expand_dims(tensor, 0)
      assert tensor.ndim == 3
    # full_name is ensured to be unique.
    # reduction function is "overwrite" if layer is called multiple times.
    self.sow(SUMMARIES, full_name, tensor, reduce_fn=lambda x, y: y)

  @nn.nowrap
  def add_aux_loss(self, name: str, value: JTensor, weight=None):
    # Accumulate by summing aux_loss.
    if weight is None:
      weight = jnp.ones_like(value)

    def reduce_fn(x, y):
      assert isinstance(x, AuxLossStruct)
      assert isinstance(y, AuxLossStruct)
      return AuxLossStruct(value=x.value + y.value, weight=x.weight + y.weight)

    self.sow(
        AUX_LOSS,
        name,
        AuxLossStruct(value, weight),
        init_fn=lambda: AuxLossStruct(0.0, 0.0),  # pytype: disable=wrong-arg-types  # jax-ndarray
        reduce_fn=reduce_fn)

  @nn.nowrap
  def next_prng_key(self, name=RANDOM):
    return self.make_rng(name)

  @property
  def jax_context(self) -> JaxContext:
    return cur_jax_context()

  @property
  def do_eval(self) -> bool:
    return self.jax_context.do_eval

  @nn.nowrap
  def get_var(self, name: str) -> Any:
    assert self.has_variable(NON_TRAINABLE, name)
    return self.get_variable(NON_TRAINABLE, name)

  @nn.nowrap
  def update_var(self, name: str, new_val: JTensor) -> None:
    """Update var 'name' in the forward pass."""
    old_val = self.get_var(name)
    if self.is_mutable_collection(NON_TRAINABLE) and not self.is_initializing():
      asserts.eq(old_val.shape, new_val.shape)
      self.put_variable(NON_TRAINABLE, name, new_val)

  @nn.nowrap
  def get_decode_state(self, name: str) -> JTensor:
    """Looks up decode state with given name.

    The decode state is batch major.
    Args:
      name: Variable name in decoder cache.

    Returns:
      Decode state with the given name.
    """
    assert self.has_variable(DECODE_CACHE, name), name
    return self.get_variable(DECODE_CACHE, name)

  @nn.nowrap
  def update_decode_state(self, name: str, new_state: JTensor) -> None:
    """Updates decode state with the new value.

    This function can be used to initialize decode state as well. When
    DECODE_CACHE
    is immutable, this is a no-op.

    Args:
      name: Variable name in decoder cache.
      new_state: New state to update.
    """
    if self.is_mutable_collection(DECODE_CACHE):
      self.put_variable(DECODE_CACHE, name, new_state)

  @nn.nowrap
  def create_quantized_variable(
      self,
      name: str,
      weight_hparams: WeightHParams,
      scale_shape: Sequence[int],
      dtype: jnp.dtype = jnp.int8,
      use_symmetric: bool = True,
  ):
    """Creates quantized variables, a pair of weight and scale tensors.

    `name` will be name of the weight tensor; `name` + `_quantized_scale` and
    `name` + `_quantized_zp` will be the names of the scale tensor and the zero
    point tensor, respectively.

    Only the shape and mesh for weight_hparams are used. The scale and the zero
    point have the same shape, assuming per-channel quantization.

    Currently supports only int8 weight types.

    Args:
      name: Variable name for the weight tensor.
      weight_hparams: HParams for weight.
      scale_shape: Shape of the scales.
      dtype: Data type of the quantized weight tensor.
      use_symmetric: If False, additionally create a variable for the zero point
        used for asymmetric weight quantization.
    """

    quantized_weight_hparams = weight_hparams.clone()
    quantized_weight_hparams.dtype = dtype
    quantized_weight_hparams.init = WeightInit.Constant(0)
    self.create_variable(name=name, var_hparams=quantized_weight_hparams)
    self.create_variable(
        name=name + QUANTIZED_SCALE_NAME_POSTFIX,
        var_hparams=WeightHParams(shape=scale_shape),
    )
    if not use_symmetric:
      self.create_variable(
          name=name + QUANTIZED_ZP_NAME_POSTFIX,
          var_hparams=WeightHParams(shape=scale_shape),
      )

  @nn.nowrap
  def get_quantized_weight(
      self, name: str, use_symmetric: bool = True
  ) -> Tuple[JTensor, JTensor, Optional[JTensor]]:
    """Gets quantized variables.

    Gets a tuple of weight, scale, and possibly zero point tensors. To be used
    together with `create_quantized_variable`.

    `name` will be name of the weight tensor; assumes scale and zero point
    tensor have the postfix, `_quantized_scale` and `_quantized_zp`,
    respectively.

    Args:
      name: Variable name for the weight tensor.
      use_symmetric: If False (weight quantized asymmetrically), return the zero
        point along with quantized weight and scale.

    Returns:
      A Tuple of three elements for weight Tensor, scale Tensor, and zero point
      Tensor.
    """

    scale_name = name + QUANTIZED_SCALE_NAME_POSTFIX
    zp_name = name + QUANTIZED_ZP_NAME_POSTFIX
    zp = None if use_symmetric else self.theta[zp_name]
    return self.theta[name], self.theta[scale_name], zp

  @nn.nowrap
  def create_variable(self,
                      name: str,
                      var_hparams: WeightHParams,
                      trainable: bool = True) -> Any:
    """Create a variable of this layer according to the parameter `var_hparams`.

    E.g.::

        def create_layer_variables(self):
          self.create_variable(
              'weight', WeightHParams(shape=[100, 100]))

    Args:
      name: Variable name which is used as the key into vars/theta.
      var_hparams: WeightHParams used to create the variable.
      trainable: whether or not this param is trainable.

    Returns:
      The newly created variable.
    """
    if hasattr(self, name):
      raise AttributeError(
          f'{self.__class__} can not create a new variable named {name!r} '
          'because it already has a field with that name.')

    var_hparams = var_hparams.clone()

    # If users did not specify init and dtype for var_hparams, fill in from
    # self.
    if var_hparams.init is None:
      var_hparams.init = self.params_init.clone()
    if var_hparams.dtype is None:
      var_hparams.dtype = self.dtype

    full_name = self.scope.path_text + '/' + name
    if self.mesh_shape is not None:
      if (len([dim for dim in var_hparams.shape if dim > 1]) > 1 and
          var_hparams.tensor_split_dims_mapping is None):
        logging.warning(
            'tensor_split_dims_mapping missing for %s: shape=%s',
            full_name,
            var_hparams.shape,
        )
      if var_hparams.tensor_split_dims_mapping is not None:
        assert len(var_hparams.tensor_split_dims_mapping) == len(
            var_hparams.shape)

    if var_hparams.collections is None:
      var_hparams.collections = []

    if (self.skip_lp_regularization and
        WeightHParamsCollection.SKIP_LP_REGULARIZATION
        not in var_hparams.collections):
      var_hparams.collections = var_hparams.collections + [
          WeightHParamsCollection.SKIP_LP_REGULARIZATION
      ]

    if (not trainable) and (WeightHParamsCollection.NON_TRAINABLE
                            not in var_hparams.collections):
      var_hparams.collections = var_hparams.collections + [
          WeightHParamsCollection.NON_TRAINABLE
      ]

    # Store a private copy of var_hparams.
    self._weight_hparams[name] = var_hparams.clone()

    if trainable:
      # This is a param in Flax terminology.
      def _initializer_fn(prng_key: PRNGKey):
        value = init_var(var_hparams, prng_key, full_name)
        return BoxedParam(value=value, meta=var_hparams)

      self.param(name, _initializer_fn)
      # Add var to the private theta name set for checks.
      self._theta.add(name)
      return getattr(self.theta, name)
    else:

      def _initializer_fn():
        # Use params rng stream to avoid having caller to provide one for
        # non-trainable variables.
        prng_key = self.make_rng(PARAMS)
        value = init_var(var_hparams, prng_key, full_name)
        return BoxedParam(value=value, meta=var_hparams)

      # Non-trainable variables go into Flax nontrainable var collection.
      self.variable(NON_TRAINABLE, name, _initializer_fn)
      return self.get_var(name)

  @nn.nowrap
  def create_child(
      self, name: str, params: pax_fiddle.Config[BaseLayerT]
  ) -> BaseLayerT:
    """Creates a sub layer.

    The created sub layer can be accessed by `name`. E.g.::

        self.create_child('foo', foo_params)
        self.foo(...)

    If the layer does not have a name set, i.e. foo_params.name is None, then
    its name will be set to `name`.

    Args:
      name: Sub layer name which is used as the key into vars/theta.
      params: `Hyperparams` object to instantiate a layer.

    Returns:
      The created sub layer, or makes the sub layer an assess of this layer.
    """
    self._check_child_layername_conflict(name)
    child = self._create_child(name, params)
    if self._state.in_setup:
      setattr(self, name, child)
    return child

  @nn.nowrap
  def create_children(
      self, name: str, params: Sequence[pax_fiddle.Config[BaseLayer]]
  ) -> Sequence[BaseLayer]:
    """Creates a list of sub layers.

    The created sub layer list can be accessed by `name`. E.g.::

        self.create_children('foo', ...)
        self.foo[10](...)

    Args:
      name: The name for the sub layers, which is used as the key into
        vars/theta.
      params: a list of `Hyperparams` objects to create.

    Returns:
      The created sub layers, or makes the sub layers an assess of this layer.
    """
    assert isinstance(params, Sequence)
    uid = itertools.count()
    self._check_child_layername_conflict(name)
    children = [self._create_child(f'{name}_{next(uid)}', p) for p in params]
    if self._state.in_setup:
      setattr(self, name, children)
    return children

  @nn.nowrap
  def _create_child(
      self, name: str, params: pax_fiddle.Config[BaseLayer]
  ) -> BaseLayer:
    """Creates and returns a child (w/o adding it as an attribute of `self`)."""
    if not isinstance(params, pax_fiddle.Config):
      msg = ('Expected templates for `create_child` to be Fiddle Configs; got '
             f'{type(params)}.')
      if isinstance(params, BaseLayer):
        msg += (' This may be caused by a missing DoNotBuild tag on a field '
                'that contains a Fiddle Config.')
      raise ValueError(msg + f'\nparams={params}')
    if name is self._private_children:
      raise ValueError(
          f'Child `{name}` already exists: make sure to use unique child names.'
      )

    p = params.clone()
    self.copy_base_hparams(self, p)  # mutates p in place.
    p.name = name
    child = instantiate_layer(p, self.scope.root)
    self._private_children[name] = child
    return child

  @nn.nowrap
  def _check_child_layername_conflict(self, name: str):
    """Registers child creation with LayerRegistry."""
    if name in self._hparam_fields:
      raise AttributeError(
          f'{self.__class__} can not create a new child named {name!r} because '
          f'it already has a field with that name.')

  @nn.nowrap
  def _cast_to_fprop_dtype(self, value: Any) -> Any:
    """Casts values to the desired dtype."""

    def _cast(x):
      if x is None:
        return None
      if self.fprop_dtype != x.dtype:
        if jnp.issubdtype(x.dtype, jnp.floating):
          return x.astype(self.fprop_dtype)
      return x

    return jax.tree_util.tree_map(_cast, value)

  def quantize_weight(self) -> NestedJTensor:
    """Quantize the current layer and its children layer(s).

    Returns:
      a nested map from names to quantized weights.
    """
    return self._quantize_fn(return_pspec=False)

  def quantized_partition_specs(self) -> Any:
    """Get quantization spec for the current layer and its children layer(s).

    Returns:
      a nested map from names to partition spec.
    """
    return self._quantize_fn(return_pspec=True)

  def _quantize_fn(self, return_pspec: bool) -> Union[NestedJTensor, Any]:
    """quantize_weight() quantize the current layer and its children layer(s).

    Quantization applies to only PARAMS and NON_TRAINABLE collection.

    a) the provided default implementation here simply returns the weight
       verbatim.
    b) concrete layers should overwrite the default implementation with
       potentially different quantization strategies. They should take care of
       quantization of their children's layers, too.

    Args:
      return_pspec: a boolean to control if returning ParititionSpecs for
      quantized tensors.
          If True, returns the partition specs.
          If False, returns quantized tensors.

    Returns:
      a nested map from names to quantized layer or partition spec.
    """
    res = {}
    # collections to quantize.
    targets = [PARAMS, NON_TRAINABLE]
    for name, child in self._private_children.items():
      # example child_res {'params': {a:{}, b:{}}, 'non-trainable':{a:{}}}
      if return_pspec:
        child_res = child.quantized_partition_specs()
      else:
        child_res = child.quantize_weight()
      for child_target in child_res:
        if child_target not in res:
          res[child_target] = {}
        res[child_target][name] = child_res[child_target]
    for target in targets:
      if target not in self.variables:
        continue
      for var_name, var_val in self.variables[target].items():
        if var_name in self._private_children:
          continue
        if target not in res:
          res[target] = {}
        if return_pspec:
          var_val = _weight_hparam_to_pspec(self._weight_hparams[var_name],
                                            self.mesh_axis_names)
        res[target][var_name] = var_val
    return res

  @classmethod
  def config(cls, **kwargs) -> pax_fiddle.Config:
    return pax_fiddle.Config(cls, **kwargs)


def _is_template_type(typ):
  """Returns true if `typ` is a type expression for a template."""
  # if `typ` is a generic type, then `origin` is its unparameterized type.
  # E.g., if `typ = tuple[int, int]`, then `origin = tuple`.  If `typ` is not
  # generic, then `origin` is `None`.  Also, if we are using Python 3.7 or
  # earlier, then `typing.get_origin` doesn't exist, so `origin` will be `None`.
  origin = typing.get_origin(typ) if hasattr(typing, 'get_origin') else None

  if (origin is None and isinstance(typ, type) and
      issubclass(typ, pax_fiddle.Config)):
    return True
  if isinstance(typ, _FiddleHParamsClassStub):
    return True
  if origin == pax_fiddle.Config:
    return True
  if any(_is_template_type(arg) for arg in typing.get_args(typ)):
    return True
  return False


def assert_has_shape(t: JTensor, shape: Sequence[int]) -> None:
  asserts.eq(t.ndim, len(shape))
  value_str1 = f't.shape={t.shape}'
  value_str2 = f'shape={shape}'
  for i in range(t.ndim):
    if shape[i] != -1:
      asserts.eq(
          t.shape[i], shape[i],
          value_str1=value_str1,
          value_str2=value_str2)


def compatible_hparams(
    hparams1: Union[base_hyperparams.HParams, pax_fiddle.Config],
    hparams2: Union[base_hyperparams.HParams, pax_fiddle.Config]) -> bool:
  """Returns True if hparams1 and hparams2 are compatible to each other.

  The current definition of "compatible" are two params are identical except
  for their names.

  Args:
    hparams1: hyper-params for layer1
    hparams2: hyper-params for layer2

  Returns:
    True if two hparams are fully compatible.
  """
  p1 = hparams1.clone()
  p1.name = ''
  p2 = hparams2.clone()
  p2.name = ''
  if isinstance(p1, pax_fiddle.Config) or isinstance(p2, pax_fiddle.Config):
    if not (isinstance(p1, pax_fiddle.Config) and
            isinstance(p2, pax_fiddle.Config)):
      raise ValueError('Expected hparams1 and hparams2 to have the same type; '
                       f'got {hparams1!r} and {hparams2!r}')
    return p1 == p2
  else:
    return p1.to_text() == p2.to_text()


class _WrapperLayer(BaseLayer):
  """A simple wrapper layer."""

  cld_tpl: Optional[pax_fiddle.Config[BaseLayer]] = template_field(None)

  def setup(self) -> None:
    # create child under the name space of 'name'.
    # This implicitly set p.cld.name to name as well.
    self.create_child(self.name, self.cld_tpl)
    self.cld = getattr(self, self.name)


def get_template_fields(
    template: Union[BaseHyperParams, pax_fiddle.Config]) -> List[str]:
  """Returns the names of the configurable fields for `template`.

  Does not include `"cls"`.

  Args:
    template: The HParams or fdl.Config whose field names should be returned.
  """
  if isinstance(template, pax_fiddle.Config):
    return list(
        fdl.ordered_arguments(
            template, include_defaults=True, include_unset=True))
  elif isinstance(template, BaseHyperParams):
    return [
        field.name
        for field in dataclasses.fields(template)  # pytype: disable=wrong-arg-types  # re-none
        if field.name != 'cls'
    ]
  else:
    raise TypeError(f'Unexpected type for template: {type(template)}')


# Backwards-compatibility aliases.
# TODO(b/249483164): Remove these once the HParams->Fiddle migration is done.
BaseLayerApi = BaseLayer
