# Copyright 2021 Alibaba Group Holding Limited. All Rights Reserved.
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
# =============================================================================
""" Contains the BatchGraphFlow which induces a batch of SubGraphs(BatchGraph)
using given query and convert it to tensor format.
"""
import numpy as np
import tensorflow as tf

from collections import OrderedDict
from enum import Enum

from graphlearn.python.errors import OutOfRangeError
from graphlearn.python.gsl.dag_dataset import Dataset
from graphlearn.python.nn.tf.data.batchgraph import BatchGraph
from graphlearn.python.nn.tf.data.utils.induce_graph_with_edge import \
  induce_graph_with_edge


class SubKeys(Enum):
  POS_SRC = 0
  NGE_SRC = 1 # not support yet.
  POS_DST = 2
  NEG_DST = 3
  POS_EDGE = 4 # not support yet.
  NEG_EDGE = 5 # not support yet.


class BatchGraphFlow(object):
  def __init__(self,
               query,
               induce_func=induce_graph_with_edge,
               induce_additional_spec=None,
               window=5):
    """Creates a BatchGraphFlow object which is used to convert GSL
    subgraph query results to BatchGraphs using given induce_func.

    Args:
      query: GSL query, which must contain `SubKeys` as aliases.
      induce_func: `SubGraph` inducing function, it should be either 
        induce with edge or induce with node. The induce with edge function
        requires 4 args (src, dst, src_nbrs, dst_nbrs), and the induce with 
        node function requires 2 args (src, src_nbrs). 
        This function should be overridden when you need implement 
        your own SubGraph inducing procedure.
      induce_additional_spec: A dict to describe the additional data of 
        BatchGraph which is generated by the induce_func. Each key is the name 
        of additional data, and values is a list [types, shapes], which are
        tf.dtype and tf.TensorShape instance to describe the tensor format 
        types and shapes of additional data.
      window: dataset capacity.
    """
    self._dag = query
    self._ds = Dataset(query, window)
    self._iterator = None
    self._induce_func = induce_func
    self._additional_spec = induce_additional_spec
    self._additional_keys = []
    if self._additional_spec is not None:
      self._additional_keys = self._additional_spec.keys()
    self._pos_graph, self._neg_graph = self._build()

  @property
  def iterator(self):
    return self._iterator
  
  def batchgraph(self, alias):
    if (alias == SubKeys.POS_SRC) or (alias == SubKeys.POS_DST):
      return self._pos_graph
    elif alias == SubKeys.NEG_DST:
      return self._neg_graph
    else:
      raise ValueError("alias must be one of [SubKeys.POS_SRC, " 
                       "SubKeys.POS_DST, SubKeys.NEG_DST]")

  def _build(self):
    output_types, output_shapes = self._batchgraph_types_and_shapes()
    pos_size = len(output_types)
    if SubKeys.NEG_DST in self._dag.list_alias():
      neg_types, neg_shapes = self._batchgraph_types_and_shapes()
      output_types += neg_types
      output_shapes += neg_shapes
    #TODO(baole): supports generator with node.
    dataset = tf.data.Dataset.from_generator(
      self._sample_generator_with_edge,
      output_types,
      output_shapes)
    self._iterator = dataset.make_initializable_iterator()
    value = self._iterator.get_next()
    pos_graph = BatchGraph.from_tensors(value[0:pos_size], 
      (self._dag.get_node(SubKeys.POS_SRC).type, 
       self._dag.get_node(SubKeys.POS_SRC).spec),
      additional_keys = self._additional_keys)
    neg_graph = None
    if SubKeys.NEG_DST in self._dag.list_alias():
      neg_graph = BatchGraph.from_tensors(value[pos_size:],
        (self._dag.get_node(SubKeys.POS_SRC).type, 
         self._dag.get_node(SubKeys.POS_SRC).spec),
        additional_keys = self._additional_keys)
    return pos_graph, neg_graph

  def _sample_generator_with_edge(self):
    while True:
      try:
        values = self._ds.next()
        pos_src = values[SubKeys.POS_SRC]
        # TODO(baole): support multi-hops
        src_nbrs = values[self._dag.get_node(SubKeys.POS_SRC).\
          pos_downstreams[0].get_alias()]
        if SubKeys.POS_DST in self._dag.list_alias():
          pos_dst = values[SubKeys.POS_DST]
          dst_nbrs = values[self._dag.get_node(SubKeys.POS_DST).\
            pos_downstreams[0].get_alias()]
        else: # fake a src-src edge.
          pos_dst, dst_nbrs = pos_src, src_nbrs
        subgraphs = self._induce_func(pos_src, pos_dst, src_nbrs, dst_nbrs)
        pos_graph = BatchGraph.from_graphs(subgraphs, 
          additional_keys=self._additional_keys)
        flatten_list = pos_graph.flatten()
        # negative samples.
        if SubKeys.NEG_DST in self._dag.list_alias():
          neg_dst = values[SubKeys.NEG_DST]
          neg_dst_nbrs = values[self._dag.get_node(SubKeys.NEG_DST).\
            pos_downstreams[0].get_alias()]
          neg_subgraphs = self._induce_func(pos_src, neg_dst, 
            src_nbrs, neg_dst_nbrs)
          neg_graph = BatchGraph.from_graphs(neg_subgraphs, 
            additional_keys=self._additional_keys)
          flatten_list.extend(neg_graph.flatten())
        yield tuple(flatten_list)
      except OutOfRangeError:
        break

  def _batchgraph_types_and_shapes(self):
    output_types, output_shapes = tuple(), tuple()
    # edge index
    output_types += tuple([tf.int32])
    output_shapes += tuple([tf.TensorShape([2, None])])
    # nodes
    node_types, node_shapes = \
      self._nodes_types_and_shapes(SubKeys.POS_SRC) # homo graph.
    output_types += tuple(node_types)
    output_shapes += tuple(node_shapes)
    # node_graph_id
    output_types += tuple([tf.int64])
    output_shapes += tuple([tf.TensorShape([None])])
    for key in self._additional_keys:
      output_types += tuple([self._additional_spec[key][0]])
      output_shapes += tuple([self._additional_spec[key][1]])
    return output_types, output_shapes

  def _nodes_types_and_shapes(self, alias):
    # ids
    ids_types = [tf.int64]
    ids_shapes = [tf.TensorShape([None])]
    node = self._dag.get_node(alias)
    node_spec = node.spec

    shapes = []
    feats = ('int_attr_num', 'float_attr_num', 'string_attr_num')
    types = np.array([tf.int32, tf.float32, tf.string])
    masks = [False] * len(types)
    for idx, feat in enumerate(feats):
      feat_num = getattr(node_spec, feat)
      if feat_num > 0:
        masks[idx] = True
        shapes.append(tf.TensorShape([None, feat_num]))
    return ids_types + list(types[masks]), ids_shapes + shapes