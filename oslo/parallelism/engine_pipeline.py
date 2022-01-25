import time

import torch
from anytree import Node
from transformers import GPT2LMHeadModel
from oslo.parallelism.utils import num_params, is_available_tracing

BATCH_DIMENSIONS = {
    "past_key_values": 0,
}


class PipelineParallelEngine(object):
    """
    For more information of the implementation, see the following paper.

    References:
        Amazon SageMaker Model Parallelism: A General and Flexible Framework for Large Model Training
        https://arxiv.org/abs/2111.05972
    """

    def __init__(self, model, mpu, memory_computation_balance_factor=1.0):
        self.model = model
        self.mpu = mpu

        # 1. construct the tree of node modules
        self.root_node = Node(
            name="",
            full_name="",
            module=self.model,
        )
        self._construct_tree(
            module=self.model,
            node=self.root_node,
        )

        # 2. compute the partitioning cost
        cost_estimator = PartitioningCostEstimator(
            root_node=self.root_node,
            alpha=memory_computation_balance_factor,
        )
        cost_estimator.compute_cost()

    def parallelize(self):
        def test(node):
            print(node.full_name, node.cost)
            for child in node.children:
                test(child)

        test(self.root_node)

    def _construct_tree(self, module, node):
        for child_name, child_module in module.named_children():
            self._construct_tree(
                module=child_module,
                node=Node(
                    name=child_name,
                    full_name=child_name
                    if len(node.name) == 0
                    else f"{node.full_name}.{child_name}",
                    module=child_module,
                    parent=node,
                ),
            )


class PartitioningCostEstimator(object):
    """
    Partitioning cost estimator

    1. computation cost: supports only the cpu time estimating in this version.
    2. memory cost: computes memory cost via the number of parameters of the module.
    """

    def __init__(self, root_node, alpha):
        self.root_node = root_node
        self.alpha = alpha
        self.hooks = []

        self.orig_gradient_checkpointing_status = (
            self.root_node.module.is_gradient_checkpointing
        )

        # enable gradient checkpointing for memory safer tracing.
        if self.root_node.module.supports_gradient_checkpointing:
            self.root_node.module.gradient_checkpointing_enable()

        # prevent tracing for very large model.
        if self.alpha < 1.0:
            if not is_available_tracing(self.root_node.module):
                print(
                    "This model is too large to trace on the CPU."
                    "turn off computation cost estimating."
                )
                self.use_computation_cost = False
            else:
                self._trace_computation_cost()

    def _add_computation_cost_hooks(self, node):
        def pre_hook(*args, **kwargs):
            setattr(node, "execution_time_before_forwarding", time.time())

        def post_hook(*args, **kwargs):
            setattr(
                node,
                "computation_cost",
                time.time() - getattr(node, "execution_time_before_forwarding"),
            )
            delattr(node, "execution_time_before_forwarding")

        self.hooks.append(
            {
                "pre_hook": node.module.register_forward_pre_hook(pre_hook),
                "post_hook": node.module.register_forward_hook(post_hook),
            }
        )

        for child in node.children:
            self._add_computation_cost_hooks(child)

    def _trace_computation_cost(self):
        # 1. tracing the model
        with torch.no_grad():
            dummy_inputs = self.root_node.module.dummy_inputs
            dummy_inputs["use_cache"] = False  # for checkpointing
            self._add_computation_cost_hooks(self.root_node)
            self.root_node.module(**dummy_inputs)

        # 2. removing hooks
        for hooks in self.hooks:
            hooks["pre_hook"].remove()
            hooks["post_hook"].remove()

        # 3. turn off gradient checkpointing
        if not self.orig_gradient_checkpointing_status:
            self.root_node.module.gradient_checkpointing_disable()

    def _compute_node_cost(self, node):
        # 1. compute memory cost
        memory_cost = num_params(node.module)

        # 2. compute computation cost if available
        computation_cost = (
            node.computation_cost if hasattr(node, "computation_cost") else 0.0
        )

        # 3. compute total cost
        total_cost = (self.alpha * memory_cost) + ((1 - self.alpha) * computation_cost)

        setattr(node, "unnormalized_cost", total_cost)

        if hasattr(node, "computation_cost"):
            delattr(node, "computation_cost")

    def _compute_cost(self, node):
        if not hasattr(self.root_node, "unnormalized_cost"):
            # 1. compute cost for root node
            self._compute_node_cost(self.root_node)
        else:
            # 2. compute cost for children nodes
            self._compute_node_cost(node)

        # 3. do recursion
        for child in node.children:
            self._compute_cost(child)

    def _normalize_cost(self, node):
        if not hasattr(self.root_node, "cost"):
            # 1. normalize cost for root node
            setattr(self.root_node, "cost", 1.0)
        else:
            # 2. normalize cost for children nodes
            root_cost = getattr(self.root_node, "unnormalized_cost")
            node_cost = getattr(node, "unnormalized_cost")
            setattr(node, "cost", node_cost / root_cost)

        # 3. do recursion
        for child in node.children:
            self._normalize_cost(child)

    def compute_cost(self):
        # 1. compute cost
        self._compute_cost(self.root_node)

        # 2. normalize cost
        self._normalize_cost(self.root_node)


if __name__ == "__main__":
    model = GPT2LMHeadModel.from_pretrained("gpt2")
    # mpu = MPU(tensor_parallel_size=1, pipeline_parallel_size=4)
    engine = PipelineParallelEngine(model, None, memory_computation_balance_factor=0.1)
    engine.parallelize()
