# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Tests for quantized operations."""

from typing import Any, Dict, Sequence

from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import numpy as jnp
import numpy as np
from praxis import base_layer
from praxis import pax_fiddle
from praxis import test_utils
from praxis.layers.quantization import aqt
from praxis.layers.quantization import operations


class QuantizationUtilsTest(test_utils.TestCase):

  def setUp(self):
    super().setUp()
    np.random.seed(123456)

  @parameterized.named_parameters(
      ('regular_eqn', 'ab,bc->ac'),
      ('eqn_with_dot', '...y,yz->...z'),
  )
  def test_quantized_einsum(self, eqn):
    x = jnp.array([[1.0, 2.0, 3.0], [4.0, 1.0, 2.0]], dtype=jnp.bfloat16)
    w = jnp.array([[1, 2, 1], [2, 1, 2], [1, 3, 1]], dtype=jnp.int8)
    s = jnp.array([0.1, 0.2, 0.3], dtype=jnp.bfloat16)

    ret = operations.einsum(eqn, x, w, s)
    expected = jnp.array([[0.800781, 2.60938, 2.40625], [0.800781, 3, 2.40625]],
                         dtype=jnp.bfloat16)
    self.assertArraysEqual(ret, expected)

  def test_quantized_einsum_with_expand_dim(self):
    # pylint: disable=invalid-name
    A, B, D, K, N, H = 6, 4, 5, 3, 7, 2
    # pylint: enable=invalid-name
    x = jnp.ones([A, B, D], dtype=jnp.bfloat16)
    w = jnp.ones([K, D, N, H], dtype=jnp.int8)
    s = jnp.ones([K, N, H], dtype=jnp.bfloat16)

    ret = operations.einsum('ABD,KDNH->KABNH', x, w, s)
    expected = jnp.ones([K, A, B, N, H], dtype=jnp.bfloat16) * D
    self.assertArraysEqual(ret, expected)

  @parameterized.named_parameters(
      ('eqn_with_dot', '...y,yz->...z'),
  )
  def test_quantized_einsum_with_zp(self, eqn):
    x = jnp.array([[1.0, 2.0, 3.0], [4.0, 1.0, 2.0]], dtype=jnp.bfloat16)
    w = jnp.array([[1, 2, 1], [2, 1, 2], [1, 3, 1]], dtype=jnp.int8)
    s = jnp.array([0.1, 0.2, 0.3], dtype=jnp.bfloat16)
    zp = jnp.array([-0.5, 3.2, 2.7])

    ret = operations.einsum(eqn, x, w, s, zp)
    expected = jnp.array(
        [[3.800781, -16.590626, -13.793751], [4.300781, -19.4, -16.49375]],
        dtype=jnp.float32,
    )
    self.assertAllClose(ret, expected, rtol=0.02, atol=0.02)


class ReducePrecisionEinsumTest(test_utils.TestCase):

  def setUp(self):
    super().setUp()
    np.random.seed(1234567)

  @parameterized.named_parameters(
      ('eqn1', 'ab,bc->ac', (4, 3), (3,), ()),
      ('eqn2', '...y,yz->...z', (6, 5), (5,), ()),
      ('eqn3', 'ABD,KDNH->KABNH', (2, 3, 4, 5), (2, 4, 5), (1)),
  )
  def test_reduce_einsum_weight_precision(self, eqn, w_shape,
                                          expected_scale_shape, expand_dims):

    weight = np.random.normal(1.5, 2.0, w_shape).astype(np.float32)
    reduced_weight, scale, _ = operations.reduce_einsum_weight_precision(
        eqn, weight)
    self.assertEqual(scale.shape, expected_scale_shape)
    if expand_dims:
      scale = jnp.expand_dims(scale, expand_dims)
    self.assertAllClose(
        weight,
        jnp.multiply(reduced_weight, scale).astype(jnp.float32),
        rtol=0.02,
        atol=0.02,
    )
    for use_symmetric in [True, False]:
      weight_nudged = operations.fakequant_einsum(
          eqn, weight, use_symmetric=use_symmetric
      )
      self.assertAllClose(weight, weight_nudged, rtol=0.02, atol=0.02)

  def test_percentile(self):
    weight = np.random.normal(-2.0, 2.0, (4, 3)).astype(np.float32)
    reduced_weight, scale, _ = operations.reduce_einsum_weight_precision(
        'ab,bc->ac', weight, percentile=0.9
    )
    # Large value discrepancy is expected since we use 0.9 percentile.
    # This just makes sure the percentile logic is correct. In practice, 0.9 is
    # small for any real use case.
    tol = 0.5
    self.assertEqual(scale.shape, (3,))
    self.assertAllClose(
        weight,
        jnp.multiply(reduced_weight, scale).astype(jnp.float32),
        rtol=tol,
        atol=tol,
    )

  def test_reduce_activation_precision(self):
    act = np.random.normal(-1.0, 1.0, [10, 100]).astype(np.float32)
    act_nudged = operations.fakequant_activation(act)
    self.assertAllClose(act, act_nudged, rtol=0.02, atol=0.02)


class DotGeneral(base_layer.BaseLayer):
  lhs_prec: int = 8
  rhs_prec: int = 8
  add_scale_eps: bool = False
  use_symmetric: bool = True

  def setup(self):
    self.create_child(
        'lhs_quantizer',
        pax_fiddle.Config(
            aqt.TensorQuantizer,
            name='lhs_quantizer',
            precision=self.lhs_prec,
            add_scale_eps=self.add_scale_eps,
        ),
    )
    self.create_child(
        'rhs_quantizer',
        pax_fiddle.Config(
            aqt.TensorQuantizer,
            name='rhs_quantizer',
            precision=self.rhs_prec,
            add_scale_eps=self.add_scale_eps,
            use_symmetric=self.use_symmetric,
        ),
    )

  def __call__(self, lhs, rhs, is_eval=False):
    if not is_eval:
      self.lhs_quantizer.update(lhs)
      self.rhs_quantizer.update(rhs)

    def dot_general(lhs, rhs, dimension_numbers, eqn=None):
      if not self.use_symmetric:
        assert eqn is not None
      return operations.dot_general(
          lhs,
          rhs,
          self.lhs_quantizer,
          self.rhs_quantizer,
          dimension_numbers,
          is_eval,
          eqn,
      )

    return dot_general


def _generate_dimension_numbers() -> Sequence[Dict[str, Any]]:
  """Generates arbitrary dimension numbers for a tensor of shape (2, 2, 2)."""
  keys = ['testcase_name', 'dimension_numbers']
  # ((lhs_contracting_dims, rhs_contracting_dims), (lhs_batch_dims,
  # rhs_batch_dims))
  cases = [
      ('batch_matmul', (((2,), (1,)), ((0,), (0,)))),
      ('one_cont_two_batch_dims', (((2,), (2,)), ((0, 1,), (0, 1,)))),
      ('two_cont_one_batch_dims', (((1, 2), (1, 2)), ((0,), (0,)))),
      ('one_contracting_dims', (((2,), (1,)), ((), ()))),
      ('two_contracting_dims', (((1, 2), (1, 2)), ((), ()))),
  ]
  return [dict(zip(keys, vals)) for vals in cases]


class AqtDotGeneralTest(test_utils.TestCase):

  def setUp(self):
    super().setUp()
    np.random.seed(0)

  def get_dot_general_module(
      self,
      lhs,
      rhs,
      lhs_prec,
      rhs_prec,
      add_scale_eps=False,
      is_eval=False,
      use_symmetric=True,
  ):
    p_dot_general = pax_fiddle.Config(
        DotGeneral,
        name='dot_general',
        lhs_prec=lhs_prec,
        rhs_prec=rhs_prec,
        add_scale_eps=add_scale_eps,
        use_symmetric=use_symmetric,
    )
    module = base_layer.instantiate(p_dot_general)
    state = module.init(jax.random.PRNGKey(0), lhs, rhs)
    return module.apply(state, lhs, rhs, is_eval, mutable=['non_trainable'])

  def basic_quant_example(self):
    lhs = np.array(
        [
            [-7.0, 4.01, 4.01],  #
            [-7.0, 0.01, -4.01],
        ],)
    q_deq_lhs = np.array(
        [
            [-6, 4, 4],  #
            [-6, 0, -4]
        ],)

    # Representable values: -1, 0, 1
    rhs = np.array(
        [
            [-1.5, 0.99],  #
            [-0.99, 0],
            [-0.01, 1.5]
        ],)
    q_deq_rhs = np.array(
        [
            [-1, 1],  #
            [-1, 0],
            [0, 1]
        ],)

    return lhs, q_deq_lhs, rhs, q_deq_rhs

  def test_basic_dot_general(self):
    lhs, q_deq_lhs, rhs, q_deq_rhs = self.basic_quant_example()

    dot_general, _ = self.get_dot_general_module(lhs, rhs, 3, 2)
    dimension_numbers = (((1,), (0,)), ((), ()))
    actual_ret = dot_general(lhs, rhs, dimension_numbers)
    expected_ret = jax.lax.dot_general(q_deq_lhs, q_deq_rhs,
                                       dimension_numbers).astype(jnp.float32)
    self.assertArraysEqual(actual_ret, expected_ret)

  @parameterized.named_parameters(_generate_dimension_numbers())
  def test_dot_general_none(self, dimension_numbers):
    """Ensures no quantization gives aqt_dot_general=lax.dot_general."""
    lhs = np.random.uniform(-1.0, 1.0, size=(2, 2, 2)).astype(np.float32)
    rhs = np.random.uniform(-1.0, 1.0, size=(2, 2, 2)).astype(np.float32)

    dot_general, _ = self.get_dot_general_module(lhs, rhs, None, None)
    actual_ret = dot_general(lhs, rhs, dimension_numbers)
    expected_ret = jax.lax.dot_general(lhs, rhs, dimension_numbers)
    self.assertArraysEqual(actual_ret, expected_ret)

  def test_scaling_stability(self):
    lhs = np.random.uniform(-1.0, 1.0, size=(2, 3)).astype(np.float32)
    # Small values cause NaN gradients if the gradients for scaling and
    # rescaling are unnecessarily propagated.
    rhs = np.random.normal(0, 1e-23, size=(3, 4)).astype(np.float32)

    dot_general, _ = self.get_dot_general_module(lhs, rhs, None, 4, True)
    matmul_dimension_numbers = [[[1], [0]], [[], []]]

    @jax.grad
    def grad_qmatmul(rhs, lhs):
      return jnp.sum(dot_general(lhs, rhs, matmul_dimension_numbers))

    gradients = grad_qmatmul(rhs, lhs)
    self.assertFalse(np.isnan(gradients).any())

  def test_dot_general_high_bitwidth(self):
    lhs = np.random.normal(size=(2, 3)).astype(np.float32)
    rhs = np.random.normal(size=(3, 4)).astype(np.float32)

    precision = 23
    matmul_dimension_numbers = [[[1], [0]], [[], []]]
    dot_general, _ = self.get_dot_general_module(lhs, rhs, precision, precision)
    actual_ret = dot_general(lhs, rhs, matmul_dimension_numbers)
    expected_ret = jax.lax.dot_general(lhs, rhs, matmul_dimension_numbers)
    self.assertAllClose(actual_ret, expected_ret)

  @parameterized.named_parameters(
      ('eqn_with_dot', '...y,yz->...z'),
  )
  def test_dot_general_with_asymmetric_quant(self, eqn):
    lhs = jnp.array([[1.0, 2.0, 3.0], [4.0, 1.0, 2.0]], dtype=jnp.float32)
    rhs = jnp.array(
        [[1.2, 2.4, 13.5], [5.3, 1.4, 3.0], [8.2, -1.1, -0.5]],
        dtype=jnp.float32,
    )
    dimension_numbers = (((1,), (0,)), ((), ()))

    dot_general, _ = self.get_dot_general_module(
        lhs, rhs, None, 8, is_eval=True, use_symmetric=False
    )
    ret = dot_general(lhs, rhs, dimension_numbers, eqn)
    expected = jnp.array(
        [[36.348434, 1.9039061, 18.109375], [26.515232, 8.781445, 55.972656]],
        dtype=jnp.float32,
    )
    self.assertAllClose(ret, expected)


def _generate_einsum_eqn() -> Sequence[Dict[str, str]]:
  """Generates arbitrary dimension numbers for a tensor of shape (2, 2, 2)."""
  keys = ['testcase_name', 'eqn']
  # ((lhs_contracting_dims, rhs_contracting_dims), (lhs_batch_dims,
  # rhs_batch_dims))
  cases = [
      ('batch_matmul', 'abc,acd->abd'),
      ('one_cont_two_batch_dims', 'abc,abc->ab'),
      ('two_cont_one_batch_dims', 'abc,abc->a'),
      ('one_contracting_dims', 'abc,dce->abde'),
      ('two_contracting_dims', 'abc,dbc->ad'),
  ]
  return [dict(zip(keys, vals)) for vals in cases]


class AqtEinsum(base_layer.BaseLayer):
  lhs_prec = None
  rhs_prec: int = 8
  add_scale_eps: bool = False
  use_symmetric: bool = True

  def setup(self):
    self.create_child(
        'lhs_quantizer',
        pax_fiddle.Config(
            aqt.TensorQuantizer, name='lhs_quantizer', precision=self.lhs_prec
        ),
    )
    self.create_child(
        'rhs_quantizer',
        pax_fiddle.Config(
            aqt.TensorQuantizer,
            name='rhs_quantizer',
            precision=self.rhs_prec,
            add_scale_eps=self.add_scale_eps,
            use_symmetric=self.use_symmetric,
        ),
    )

  def __call__(self):
    def aqt_einsum(eqn, lhs, rhs):
      return operations.aqt_einsum(
          eqn,
          lhs,
          rhs,
          lhs_quantizer=self.lhs_quantizer,
          rhs_quantizer=self.rhs_quantizer,
      )

    return aqt_einsum


class AqtEinsumTest(test_utils.TestCase):

  def setUp(self):
    super().setUp()
    np.random.seed(0)

  def get_aqt_einsum_module(
      self, rhs_prec, add_scale_eps=False, use_symmetric=True
  ):
    p_aqt_einsum = pax_fiddle.Config(
        AqtEinsum,
        name='aqt_einsum',
        rhs_prec=rhs_prec,
        add_scale_eps=add_scale_eps,
        use_symmetric=use_symmetric,
    )
    module = base_layer.instantiate(p_aqt_einsum)
    state = module.init(jax.random.PRNGKey(0))
    return module.apply(state, mutable=['non_trainable'])

  def basic_quant_example(self):
    lhs = np.array(
        [
            [-7.0, 4.01, 4.01],  #
            [-7.0, 0.01, -4.01],
        ],)
    rhs = np.array(
        [
            [-1.5, 0.99],  #
            [-0.99, 0],
            [-0.01, 1.5]
        ],)
    q_deq_rhs = np.array(
        [
            [-1, 1],  #
            [-1, 0],
            [0, 1]
        ],)

    return lhs, rhs, q_deq_rhs

  def test_basic_aqt_einsum(self):
    lhs, rhs, q_deq_rhs = self.basic_quant_example()

    aqt_einsum, _ = self.get_aqt_einsum_module(rhs_prec=2)
    eqn = 'xy,yz->xz'
    actual_ret = aqt_einsum(eqn, lhs, rhs)
    expected_ret = jnp.einsum(eqn, lhs, q_deq_rhs)
    self.assertArraysEqual(actual_ret, expected_ret)

  @parameterized.named_parameters(_generate_einsum_eqn())
  def test_aqt_einsum_noquant(self, eqn):
    lhs = np.random.uniform(-1.0, 1.0, size=(2, 2, 2)).astype(np.float32)
    rhs = np.random.uniform(-1.0, 1.0, size=(2, 2, 2)).astype(np.float32)

    aqt_einsum, _ = self.get_aqt_einsum_module(rhs_prec=None)
    actual_ret = aqt_einsum(eqn, lhs, rhs)
    expected_ret = jnp.einsum(eqn, lhs, rhs)
    self.assertArraysEqual(actual_ret, expected_ret)


if __name__ == '__main__':
  absltest.main()
