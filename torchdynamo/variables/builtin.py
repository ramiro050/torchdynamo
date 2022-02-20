import functools
import inspect
import itertools
import math
import operator
import types
from typing import Dict
from typing import List

import torch

from .. import variables
from ..allowed_functions import is_disallowed
from ..source import AttrSource
from ..source import NNModuleSource
from ..utils import Unsupported
from ..utils import check_constant_args
from ..utils import istype
from ..utils import proxy_args_kwargs
from ..utils import unimplemented
from .base import MutableLocal
from .base import VariableTracker
from .base import typestr


class BuiltinVariable(VariableTracker):
    @staticmethod
    @functools.lru_cache(None)
    def _constant_fold_functions():
        fns = {
            abs,
            all,
            any,
            bool,
            callable,
            chr,
            dict,
            divmod,
            float,
            int,
            len,
            list,
            max,
            min,
            ord,
            pow,
            repr,
            round,
            set,
            str,
            sum,
            tuple,
            type,
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
        }
        fns.update(x for x in math.__dict__.values() if isinstance(x, type(math.sqrt)))
        return fns

    def can_constant_fold_through(self):
        return self.fn in self._constant_fold_functions()

    @staticmethod
    @functools.lru_cache(None)
    def _fx_graph_functions():
        fns = {
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
        }
        return fns

    def can_insert_in_graph(self):
        return self.fn in self._fx_graph_functions()

    def __init__(self, fn, **kwargs):
        super(BuiltinVariable, self).__init__(**kwargs)
        self.fn = fn

    def __str__(self):
        return f"{self.__class__.__name__}({self.fn.__name__})"

    def python_type(self):
        return type(self.fn)

    def as_python_constant(self):
        return self.fn

    def reconstruct(self, codegen):
        name = self.fn.__name__
        assert self.fn.__module__ == "builtins"
        assert name not in codegen.tx.f_globals, "shadowed global"
        return [codegen.create_load_global(name, add=True)]

    def constant_args(self, *args, **kwargs):
        return check_constant_args(args, kwargs)

    def tensor_args(self, *args, **kwargs):
        return any(
            isinstance(i, variables.TensorVariable)
            for i in itertools.chain(args, kwargs.values())
        )

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        constant_args = check_constant_args(args, kwargs)
        tensor_args = self.tensor_args(*args, **kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())
        has_constant_handler = self.can_constant_fold_through() and constant_args
        assert isinstance(args, list)
        assert isinstance(kwargs, dict)

        if self.can_insert_in_graph() and tensor_args:
            try:
                fn = self.fn
                if self.fn is operator.iadd and isinstance(
                    args[0], variables.ConstantVariable
                ):
                    # Work around weird bug in hf_T5
                    fn, args = operator.add, [args[1], args[0]]
                return variables.TensorVariable.create(
                    tx,
                    tx.output.create_proxy(
                        "call_function",
                        fn,
                        *proxy_args_kwargs(args, kwargs),
                    ),
                    **options,
                )
            except NotImplementedError:
                unimplemented(f"partial tensor op: {self} {args} {kwargs}")

        handler = getattr(self, f"call_{self.fn.__name__}", None)
        if handler:
            try:
                result = handler(tx, *args, **kwargs)
                if result is not None:
                    return result.add_options(options)
            except TypeError as exc:
                # args aren't what we expect
                assert "argument" in str(exc), str(exc)
            except Unsupported as exc:
                if not has_constant_handler:
                    raise
                # Actually, we will handle this just fine
                exc.remove_from_stats()

        if has_constant_handler:
            # constant fold
            return variables.ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )

        return super().call_function(tx, args, kwargs)

    def _call_min_max(self, tx, a, b):
        if self.tensor_args(a, b):
            if not isinstance(a, variables.TensorVariable):
                a, b = b, a
            assert isinstance(a, variables.TensorVariable)

            # convert min/max to torch ops
            if b.is_python_constant():
                kwargs = {"min": b} if (self.fn is max) else {"max": b}
                return variables.TorchVariable(torch.clamp).call_function(
                    tx, [a], kwargs
                )
            else:
                fn = {max: torch.maximum, min: torch.minimum}[self.fn]
                return variables.TorchVariable(fn).call_function(tx, [a, b], {})

    call_min = _call_min_max
    call_max = _call_min_max

    def call_range(self, tx, *args, **kwargs):
        if self.constant_args(*args, **kwargs):
            return variables.RangeVariable(
                value=range(
                    *[x.value for x in args],
                    **{k: v.value for k, v in kwargs.items()},
                ),
            )

    def call_slice(self, tx, *args):
        return variables.SliceVariable(args)

    def _call_iter_tuple_list(self, tx, obj=None):
        cls = variables.BaseListVariable.cls_for(self.fn)
        if obj is None:
            return cls(
                [],
                mutable_local=MutableLocal(),
            )
        elif obj.has_unpack_var_sequence(tx):
            return cls(
                list(obj.unpack_var_sequence(tx)),
                mutable_local=MutableLocal(),
            )

    call_iter = _call_iter_tuple_list
    call_tuple = _call_iter_tuple_list
    call_list = _call_iter_tuple_list

    def call_zip(self, tx, *args):
        options = VariableTracker.propagate(self, args)
        if all(x.has_unpack_var_sequence(tx) for x in args):
            items = [
                variables.TupleVariable(list(item), **options)
                for item in zip(*[arg.unpack_var_sequence(tx) for arg in args])
            ]
            return variables.TupleVariable(items, **options)
        elif all(isinstance(x, variables.TensorVariable) and x.size for x in args):
            out_size = functools.reduce(min, [x.size[0] for x in args])
            items = []
            for i in range(out_size):
                items.append(
                    variables.TupleVariable(
                        [
                            BuiltinVariable(operator.getitem, **options).call_function(
                                tx, [arg, variables.ConstantVariable(i)], {}
                            )
                            for arg in args
                        ],
                        **options,
                    )
                )
            return variables.TupleVariable(items, **options)

    def call_enumerate(self, tx, arg):
        options = VariableTracker.propagate(self, arg)
        if arg.has_unpack_var_sequence(tx):
            items = [
                variables.TupleVariable(
                    [variables.ConstantVariable(idx, **options), var],
                    **options,
                )
                for idx, var in enumerate(arg.unpack_var_sequence(tx))
            ]
            return variables.TupleVariable(items, **options)

    def call_mul(self, tx, a, b):
        if isinstance(
            a, (variables.ListVariable, variables.TupleVariable)
        ) and isinstance(b, variables.ConstantVariable):
            return a.clone(items=a.items * b.as_python_constant())
        elif isinstance(
            b, (variables.ListVariable, variables.TupleVariable)
        ) and isinstance(a, variables.ConstantVariable):
            return b.clone(items=b.items * a.as_python_constant())

    def call_len(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__len__", args[1:], kwargs)

    def call_add(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__add__", args[1:], kwargs)

    def call_iadd(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__iadd__", args[1:], kwargs)

    def call_getitem(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__getitem__", args[1:], kwargs)

    def call_isinstance(self, tx, arg, isinstance_type):
        arg_type = arg.python_type()
        isinstance_type = isinstance_type.as_python_constant()
        try:
            val = issubclass(arg_type, isinstance_type)
        except TypeError:
            val = arg_type is isinstance_type
        return variables.ConstantVariable(val)

    def call_super(self, tx, a, b):
        return variables.SuperVariable(a, b)

    def call_next(self, tx, arg):
        if isinstance(arg, variables.ListIteratorVariable):
            val, next_iter = arg.next_variables()
            tx.replace_all(arg, next_iter)
            return val

    def call_hasattr(self, tx, obj, attr):
        if attr.is_python_constant():
            name = attr.as_python_constant()
            return obj.call_hasattr(tx, name)

    def call_map(self, tx, fn, seq):
        if seq.has_unpack_var_sequence(tx):
            items = [fn.call_function(tx, [x], {}) for x in seq.unpack_var_sequence(tx)]
            return variables.TupleVariable(items)

    def call_sum(self, tx, seq, **kwargs):
        if seq.has_unpack_var_sequence(tx):
            start = kwargs.pop(
                "start", variables.ConstantVariable(0)
            ).as_python_constant()
            assert not kwargs
            items = seq.unpack_var_sequence(tx)[start:]
            return BuiltinVariable(functools.reduce).call_function(
                tx,
                [
                    BuiltinVariable(operator.add),
                    variables.TupleVariable(items),
                    variables.ConstantVariable(0).add_options(self, seq),
                ],
                {},
            )

    def call_reduce(self, tx, function, iterable, initializer=None):
        if iterable.has_unpack_var_sequence(tx):
            items = iterable.unpack_var_sequence(tx)
            if initializer is None:
                value, items = items[0], items[1:]
            else:
                value = initializer
            for element in items:
                value = function.call_function(tx, [value, element], {})
            return value

    def call_getattr(
        self, tx, obj: VariableTracker, name_var: VariableTracker, default=None
    ):
        from . import ConstantVariable
        from . import GetAttrVariable
        from . import NamedTupleVariable
        from . import PythonModuleVariable
        from . import TensorVariable
        from . import TorchVariable
        from . import UserDefinedObjectVariable
        from . import UserFunctionVariable
        from . import UserMethodVariable
        from .builder import VariableBuilder

        if not name_var.is_python_constant():
            unimplemented("non-const getattr name")
        if default is not None:
            unimplemented("getattr with default")

        options = VariableTracker.propagate(self, obj, name_var)
        guards = options.get("guards", set())
        name = name_var.as_python_constant()

        if obj.source:
            source = AttrSource(obj.source, name)
            options["source"] = source
        else:
            source = None

        if isinstance(obj, variables.NNModuleVariable):
            base = tx.output.get_submodule(obj.module_key)
            base_dict = object.__getattribute__(base, "__dict__")
            class_member = True

            if not obj.source:
                unimplemented("GETATTR with no source")

            if name in base_dict:
                subobj = base_dict[name]
            elif name in base_dict["_modules"]:
                subobj = base_dict["_modules"][name]
            elif name in base_dict["_parameters"]:
                subobj = base_dict["_parameters"][name]
            elif name in base_dict["_buffers"]:
                subobj = base_dict["_buffers"][name]
            else:
                subobj = inspect.getattr_static(base, name)
                class_member = False

            if class_member:
                return VariableBuilder(tx, NNModuleSource(source))(subobj).add_guards(
                    guards
                )
            else:
                if istype(subobj, property):
                    return UserFunctionVariable(
                        subobj.fget, guards=guards
                    ).call_function(tx, [obj], {})
                elif istype(subobj, classmethod):
                    return UserMethodVariable(
                        subobj.__func__,
                        UserDefinedObjectVariable(type(base), guards=guards),
                        **options,
                    )
                elif istype(subobj, staticmethod):
                    return UserFunctionVariable(subobj.__get__(base), **options)
                elif istype(subobj, types.FunctionType):
                    return UserMethodVariable(subobj, obj, **options)
                else:
                    unimplemented(f"class property {typestr(base)} {typestr(subobj)}")

        elif isinstance(obj, (TensorVariable, NamedTupleVariable, ConstantVariable)):
            try:
                return (
                    obj.get_var_attr(tx, name).clone(source=source).add_guards(guards)
                )
            except NotImplementedError:
                return GetAttrVariable(obj, name, **options)
        elif isinstance(obj, TorchVariable):
            member = getattr(obj.value, name)
            if not is_disallowed(member):
                return TorchVariable(member, **options)
            elif ConstantVariable.is_literal(member):
                return ConstantVariable(member, **options)
            else:
                return VariableBuilder(tx, source)(member).add_guards(guards)
        elif isinstance(obj, PythonModuleVariable):
            member = obj.value.__dict__[name]
            return VariableBuilder(tx, source)(member).add_guards(guards)
        elif isinstance(obj, UserDefinedObjectVariable):
            return obj.call_method(tx, "__getattr__", [ConstantVariable(name)], {})
        elif istype(obj, UserFunctionVariable) and name in ("__name__", "__module__"):
            return ConstantVariable(
                getattr(obj.fn, name), **VariableTracker.propagate(obj)
            )
        else:
            return GetAttrVariable(obj, name, **options)