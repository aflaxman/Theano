from functools import wraps

import numpy

from theano import scalar as scal, Constant
from theano.gof import local_optimizer
from theano.tensor import (DimShuffle, get_scalar_constant_value,
                           NotScalarConstantError)

from .basic_ops import GpuFromHost, HostFromGpu
from .elemwise import GpuDimShuffle, GpuElemwise

_one = scal.constant(numpy.asarray(1.0, dtype='float64'))


def grab_cpu_scalar(v, nd):
    if v.owner is not None:
        n = v.owner
        if (isinstance(n.op, GpuDimShuffle) and
                n.op.new_order == ('x',) * nd):
            return grab_cpu_scalar(n.inputs[0])
        elif (isinstance(n.op, DimShuffle) and
              n.op.new_order == ('x',) * nd):
            return grab_cpu_scalar(n.inputs[0])
        elif isinstance(n.op, GpuFromHost):
            return grab_cpu_scalar(n.inputs[0], nd=nd)
        else:
            return None
    else:
        if (isinstance(v, Constant) and
                v.broadcastable == (True,) * nd):
            return v.dimshuffle(())


def find_node(v, cls, ignore_clients=False):
    # This digs through possibly redundant transfers to for the node
    # that has the op class specified. If ignore_clients is False (the
    # default) it will only dig through nodes that have a single
    # client.
    if v.owner is not None and (ignore_clients or len(v.clients) == 1):
        if isinstance(v.owner.op, cls):
            return v.owner
        elif (isinstance(v.owner.op, GpuFromHost) and
              v.owner.inputs[0].owner is not None and
              isinstance(v.owner.inputs[0].owner.op, HostFromGpu)):
            return find_node(v.owner.inputs[0].owner.inputs[0], cls)
        else:
            return None


def is_equal(var, val):
    # Returns True if var is always equal to val (python value), False
    # otherwise (including if var is not constant)
    try:
        v = get_scalar_constant_value(var)
        return v == val
    except NotScalarConstantError:
        return False


def alpha_merge(cls, alpha_in, beta_in, nd):
    def wrapper(maker):
        @local_optimizer([GpuElemwise])
        @wraps(maker)
        def opt(node):
            if (isinstance(node.op, GpuElemwise) and
                    node.op.scalar_op == scal.mul and
                    node.nin == 2):
                targ = find_node(node.inputs[0], cls)
                if targ is None:
                    targ = find_node(node.inputs[1], cls)
                    lr = grab_cpu_scalar(node.inputs[0], nd=nd)
                else:
                    lr = grab_cpu_scalar(node.inputs[1], nd=nd)
                if lr is None or targ is None:
                    return None
                inputs = list(targ.inputs)
                try:
                    c = get_scalar_constant_value(lr)
                    if c == 0:
                        inputs[alpha_in] = lr
                        inputs[beta_in] = lr
                    elif c == 1:
                        inputs[alpha_in] = targ.inputs[alpha_in]
                        inputs[beta_in] = targ.inputs[beta_in]
                    else:
                        inputs[alpha_in] = lr * targ.inputs[alpha_in]
                        inputs[beta_in] = lr * targ.inputs[beta_in]
                except NotScalarConstantError:
                    inputs[alpha_in] = lr * targ.inputs[alpha_in]
                    inputs[beta_in] = lr * targ.inputs[beta_in]
                return maker(targ, *inputs)
        return opt
    return wrapper


def output_merge(cls, alpha_in, beta_in, out_in, nd):
    def wrapper(maker):
        @local_optimizer([GpuElemwise])
        @wraps(maker)
        def opt(node):
            if (isinstance(node.op, GpuElemwise) and
                    node.op.scalar_op == scal.add and
                    node.nin == 2):
                targ = find_node(node.inputs[0], cls)
                W = node.inputs[1]
                if targ is None:
                    targ = find_node(node.inputs[1], cls)
                    W = node.inputs[0]
                if targ is None:
                    return None
                if not is_equal(targ.inputs[beta_in], 0.0):
                    # other cases are too complex for now
                    return None
                if W.broadcastable != targ.inputs[out_in].broadcastable:
                    # Would need to explicitly tile the output to fill
                    # the full shape here.  Disable for now.
                    return None
                inputs = list(targ.inputs)
                inputs[out_in] = W
                inputs[beta_in] = _one.clone()
                return maker(targ, *inputs)
        return opt
    return wrapper
