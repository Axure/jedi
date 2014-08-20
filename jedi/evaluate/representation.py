"""
Like described in the :mod:`jedi.parser.representation` module,
there's a need for an ast like module to represent the states of parsed
modules.

But now there are also structures in Python that need a little bit more than
that. An ``Instance`` for example is only a ``Class`` before it is
instantiated. This class represents these cases.

So, why is there also a ``Class`` class here? Well, there are decorators and
they change classes in Python 3.

Representation modules also define "magic methods". Those methods look like
``py__foo__`` and are typically mappable to the Python equivalents ``__call__``
and others. Here's a list:

====================================== ========================================
**Method**                             **Description**
-------------------------------------- ----------------------------------------
py__call__(evaluator, params: Array)   On callable objects, returns types.
py__bool__()                           Returns True/False/None; None means that
                                       there's no certainty.
py__bases__(evaluator)                 Returns a list of base classes.
py__mro__(evaluator)                   Returns a list of classes (the mro).
====================================== ========================================

__
"""
import copy
import os
import pkgutil

from jedi._compatibility import use_metaclass, unicode, Python3Method
from jedi.parser import representation as pr
from jedi.parser.tokenize import Token
from jedi import debug
from jedi import common
from jedi.evaluate.cache import memoize_default, CachedMetaClass
from jedi.evaluate import compiled
from jedi.evaluate import recursion
from jedi.evaluate import iterable
from jedi.evaluate import docstrings
from jedi.evaluate import helpers
from jedi.evaluate import param
from jedi.evaluate import flow_analysis


def wrap(evaluator, element):
    if isinstance(element, pr.Class):
        return Class(evaluator, element)
    elif isinstance(element, pr.Function):
        return Function(evaluator, element)
    elif isinstance(element, (pr.Module)) \
            and not isinstance(element, ModuleWrapper):
        return ModuleWrapper(evaluator, element)
    else:
        return element


class Executed(pr.Base):
    """
    An instance is also an executable - because __init__ is called
    :param var_args: The param input array, consist of `pr.Array` or list.
    """
    def __init__(self, evaluator, base, var_args=()):
        self._evaluator = evaluator
        self.base = base
        self.var_args = var_args

    def is_scope(self):
        return True

    def get_parent_until(self, *args, **kwargs):
        return self.base.get_parent_until(*args, **kwargs)

    @common.safe_property
    def parent(self):
        return self.base.parent


class Instance(use_metaclass(CachedMetaClass, Executed)):
    """
    This class is used to evaluate instances.
    """
    def __init__(self, evaluator, base, var_args=()):
        super(Instance, self).__init__(evaluator, base, var_args)
        if str(base.name) in ['list', 'set'] \
                and compiled.builtin == base.get_parent_until():
            # compare the module path with the builtin name.
            self.var_args = iterable.check_array_instances(evaluator, self)
        else:
            # Need to execute the __init__ function, because the dynamic param
            # searching needs it.
            with common.ignored(KeyError):
                self.execute_subscope_by_name('__init__', self.var_args)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.is_generated = False

    @property
    def py__call__(self):
        def actual(evaluator, params):
            return evaluator.execute(method, params)

        try:
            method = self.get_subscope_by_name('__call__')
        except KeyError:
            # Means the Instance is not callable.
            raise AttributeError

        return actual

    def py__bool__(self):
        # Signalize that we don't know about the bool type.
        return None

    @memoize_default()
    def _get_method_execution(self, func):
        func = get_instance_el(self._evaluator, self, func, True)
        return FunctionExecution(self._evaluator, func, self.var_args)

    def _get_func_self_name(self, func):
        """
        Returns the name of the first param in a class method (which is
        normally self.
        """
        try:
            return str(func.params[0].get_name())
        except IndexError:
            return None

    @memoize_default([])
    def get_self_attributes(self):
        def add_self_dot_name(name):
            """
            Need to copy and rewrite the name, because names are now
            ``instance_usage.variable`` instead of ``self.variable``.
            """
            n = copy.copy(name)
            n.names = n.names[1:]
            n._get_code = unicode(n.names[-1])
            names.append(get_instance_el(self._evaluator, self, n))

        names = []
        # This loop adds the names of the self object, copies them and removes
        # the self.
        for sub in self.base.subscopes:
            if isinstance(sub, pr.Class):
                continue
            # Get the self name, if there's one.
            self_name = self._get_func_self_name(sub)
            if not self_name:
                continue

            if sub.name.get_code() == '__init__':
                # ``__init__`` is special because the params need are injected
                # this way. Therefore an execution is necessary.
                if not sub.decorators:
                    # __init__ decorators should generally just be ignored,
                    # because to follow them and their self variables is too
                    # complicated.
                    sub = self._get_method_execution(sub)
            for n in sub.get_defined_names():
                # Only names with the selfname are being added.
                # It is also important, that they have a len() of 2,
                # because otherwise, they are just something else
                if unicode(n.names[0]) == self_name and len(n.names) == 2:
                    add_self_dot_name(n)

        for s in self.base.py__bases__(self._evaluator):
            if not isinstance(s, compiled.CompiledObject):
                for inst in self._evaluator.execute(s):
                    names += inst.get_self_attributes()
        return names

    def get_subscope_by_name(self, name):
        sub = self.base.get_subscope_by_name(name)
        return get_instance_el(self._evaluator, self, sub, True)

    def execute_subscope_by_name(self, name, args=()):
        method = self.get_subscope_by_name(name)
        return self._evaluator.execute(method, args)

    def get_descriptor_return(self, obj):
        """ Throws a KeyError if there's no method. """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        args = [obj, obj.base] if isinstance(obj, Instance) else [compiled.none_obj, obj]
        return self.execute_subscope_by_name('__get__', args)

    def scope_names_generator(self, position=None):
        """
        An Instance has two scopes: The scope with self names and the class
        scope. Instance variables have priority over the class scope.
        """
        yield self, self.get_self_attributes()

        for scope, names in self.base.scope_names_generator(add_class_vars=False):
            yield self, [get_instance_el(self._evaluator, self, var, True)
                         for var in names]

    def get_index_types(self, index_array):

        indexes = iterable.create_indexes_or_slices(self._evaluator, index_array)
        if any([isinstance(i, iterable.Slice) for i in indexes]):
            # Slice support in Jedi is very marginal, at the moment, so just
            # ignore them in case of __getitem__.
            # TODO support slices in a more general way.
            indexes = []

        index = helpers.FakeStatement(indexes, parent=compiled.builtin)
        try:
            return self.execute_subscope_by_name('__getitem__', [index])
        except KeyError:
            debug.warning('No __getitem__, cannot access the array.')
            return []

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'name', 'get_imports',
                        'doc', 'raw_doc', 'asserts']:
            raise AttributeError("Instance %s: Don't touch this (%s)!"
                                 % (self, name))
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s (var_args: %s)>" % \
            (type(self).__name__, self.base, len(self.var_args or []))


def get_instance_el(evaluator, instance, var, is_class_var=False):
    """
    Returns an InstanceElement if it makes sense, otherwise leaves the object
    untouched.
    """
    if isinstance(var, (Instance, compiled.CompiledObject, pr.Operator, Token,
                        pr.Module, FunctionExecution)):
        return var

    if isinstance(var, pr.Function):
        var = Function(evaluator, var)
    elif isinstance(var, pr.Class):
        var = Class(evaluator, var)

    return InstanceElement(evaluator, instance, var, is_class_var)


class InstanceElement(use_metaclass(CachedMetaClass, pr.Base)):
    """
    InstanceElement is a wrapper for any object, that is used as an instance
    variable (e.g. self.variable or class methods).
    """
    def __init__(self, evaluator, instance, var, is_class_var):
        self._evaluator = evaluator
        self.instance = instance
        self.var = var
        self.is_class_var = is_class_var

    @common.safe_property
    @memoize_default()
    def parent(self):
        par = self.var.parent
        if isinstance(par, Class) and par == self.instance.base \
                or isinstance(par, pr.Class) \
                and par == self.instance.base.base:
            par = self.instance
        else:
            par = get_instance_el(self._evaluator, self.instance, par,
                                  self.is_class_var)
        return par

    def get_parent_until(self, *args, **kwargs):
        return pr.Simple.get_parent_until(self, *args, **kwargs)

    def get_decorated_func(self):
        """ Needed because the InstanceElement should not be stripped """
        func = self.var.get_decorated_func()
        func = get_instance_el(self._evaluator, self.instance, func)
        return func

    def expression_list(self):
        # Copy and modify the array.
        return [get_instance_el(self._evaluator, self.instance, command, self.is_class_var)
                for command in self.var.expression_list()]

    def __iter__(self):
        for el in self.var.__iter__():
            yield get_instance_el(self._evaluator, self.instance, el,
                                  self.is_class_var)

    def __getitem__(self, index):
        return get_instance_el(self._evaluator, self.instance, self.var[index],
                               self.is_class_var)

    def __getattr__(self, name):
        return getattr(self.var, name)

    def isinstance(self, *cls):
        return isinstance(self.var, cls)

    def is_scope(self):
        """
        Since we inherit from Base, it would overwrite the action we want here.
        """
        return self.var.is_scope()

    def py__call__(self, evaluator, params):
        return Function.py__call__(self, evaluator, params)

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self.var)


class Wrapper(pr.Base):
    def is_scope(self):
        return True

    def is_class(self):
        return False


class Class(use_metaclass(CachedMetaClass, Wrapper)):
    """
    This class is not only important to extend `pr.Class`, it is also a
    important for descriptors (if the descriptor methods are evaluated or not).
    """
    def __init__(self, evaluator, base):
        self._evaluator = evaluator
        self.base = base

    @memoize_default(default=())
    def py__mro__(self, evaluator):
        def add(cls):
            if cls not in mro:
                mro.append(cls)

        mro = [self]
        # TODO Do a proper mro resolution. Currently we are just listing
        # classes. However, it's a complicated algorithm.
        for cls in self.py__bases__(self._evaluator):
            # TODO detect for TypeError: duplicate base class str,
            # e.g.  `class X(str, str): pass`
            add(cls)
            for cls_new in cls.py__mro__(evaluator):
                add(cls_new)
        return tuple(mro)

    @memoize_default(default=())
    def py__bases__(self, evaluator):
        supers = []
        for s in self.base.supers:
            # Super classes are statements.
            for cls in self._evaluator.eval_statement(s):
                if not isinstance(cls, (Class, compiled.CompiledObject)):
                    debug.warning('Received non class as a super class.')
                    continue  # Just ignore other stuff (user input error).
                supers.append(cls)

        if not supers:
            # Add `object` to classes (implicit in Python 3.)
            supers.append(compiled.object_obj)
        return supers

    def py__call__(self, evaluator, params):
        return [Instance(evaluator, self, params)]

    def scope_names_generator(self, position=None, add_class_vars=True):
        def in_iterable(name, iterable):
            """ checks if the name is in the variable 'iterable'. """
            for i in iterable:
                # Only the last name is important, because these names have a
                # maximal length of 2, with the first one being `self`.
                if unicode(i.names[-1]) == unicode(name.names[-1]):
                    return True
            return False

        all_names = []
        for cls in self.py__mro__(self._evaluator):
            names = []
            if isinstance(cls, compiled.CompiledObject):
                x = cls.instance_names()
            else:
                x = reversed(cls.base.get_defined_names())
            for n in x:
                if not in_iterable(n, all_names):
                    names.append(n)
            yield cls, names
        if add_class_vars:
            yield self, compiled.type_names

    def is_class(self):
        return True

    def get_subscope_by_name(self, name):
        for s in [self] + self.py__bases__(self._evaluator):
            for sub in reversed(s.subscopes):
                if sub.name.get_code() == name:
                    return sub
        raise KeyError("Couldn't find subscope.")

    @common.safe_property
    def name(self):
        return self.base.name

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'parent', 'asserts', 'raw_doc',
                        'doc', 'get_imports', 'get_parent_until', 'get_code',
                        'subscopes']:
            raise AttributeError("Don't touch this: %s of %s !" % (name, self))
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s>" % (type(self).__name__, self.base)


class Function(use_metaclass(CachedMetaClass, Wrapper)):
    """
    Needed because of decorators. Decorators are evaluated here.
    """
    def __init__(self, evaluator, func, is_decorated=False):
        """ This should not be called directly """
        self._evaluator = evaluator
        self.base_func = func
        self.is_decorated = is_decorated

    @memoize_default()
    def _decorated_func(self):
        """
        Returns the function, that is to be executed in the end.
        This is also the places where the decorators are processed.
        """
        f = self.base_func

        # Only enter it, if has not already been processed.
        if not self.is_decorated:
            for dec in reversed(self.base_func.decorators):
                debug.dbg('decorator: %s %s', dec, f)
                dec_results = self._evaluator.eval_statement(dec)
                if not len(dec_results):
                    debug.warning('decorator not found: %s on %s', dec, self.base_func)
                    return None
                decorator = dec_results.pop()
                if dec_results:
                    debug.warning('multiple decorators found %s %s',
                                  self.base_func, dec_results)
                # Create param array.
                old_func = Function(self._evaluator, f, is_decorated=True)

                wrappers = self._evaluator.execute(decorator, (old_func,))
                if not len(wrappers):
                    debug.warning('no wrappers found %s', self.base_func)
                    return None
                if len(wrappers) > 1:
                    # TODO resolve issue with multiple wrappers -> multiple types
                    debug.warning('multiple wrappers found %s %s',
                                  self.base_func, wrappers)
                f = wrappers[0]

                debug.dbg('decorator end %s', f)

        if isinstance(f, pr.Function):
            f = Function(self._evaluator, f, True)
        return f

    def get_decorated_func(self):
        """
        This function exists for the sole purpose of returning itself if the
        decorator doesn't turn out to "work".

        We just ignore the decorator here, because sometimes decorators are
        just really complicated and Jedi cannot understand them.
        """
        return self._decorated_func() \
            or Function(self._evaluator, self.base_func, True)

    def get_magic_function_names(self):
        return compiled.magic_function_class.get_defined_names()

    def get_magic_function_scope(self):
        return compiled.magic_function_class

    @Python3Method
    def py__call__(self, evaluator, params):
        if self.is_generator:
            return [iterable.Generator(evaluator, self, params)]
        else:
            return FunctionExecution(evaluator, self, params).get_return_types()

    def __getattr__(self, name):
        return getattr(self.base_func, name)

    def __repr__(self):
        dec_func = self._decorated_func()
        dec = ''
        if not self.is_decorated and self.base_func.decorators:
            dec = " is " + repr(dec_func)
        return "<e%s of %s%s>" % (type(self).__name__, self.base_func, dec)


class FunctionExecution(Executed):
    """
    This class is used to evaluate functions and their returns.

    This is the most complicated class, because it contains the logic to
    transfer parameters. It is even more complicated, because there may be
    multiple calls to functions and recursion has to be avoided. But this is
    responsibility of the decorators.
    """
    @memoize_default(default=())
    @recursion.execution_recursion_decorator
    def get_return_types(self):
        func = self.base

        if func.listeners:
            # Feed the listeners, with the params.
            for listener in func.listeners:
                listener.execute(self._get_params())
            # If we do have listeners, that means that there's not a regular
            # execution ongoing. In this case Jedi is interested in the
            # inserted params, not in the actual execution of the function.
            return []

        types = list(docstrings.find_return_types(self._evaluator, func))
        for r in self.returns:
            if isinstance(r, pr.KeywordStatement):
                stmt = r.stmt
            else:
                stmt = r  # Lambdas

            if stmt is None:
                continue

            check = flow_analysis.break_check(self._evaluator, self, r.parent)
            if check is not flow_analysis.UNREACHABLE:
                types += self._evaluator.eval_statement(stmt)
            if check is flow_analysis.REACHABLE:
                break
        return types

    @memoize_default(default=())
    def _get_params(self):
        """
        This returns the params for an TODO and is injected as a
        'hack' into the pr.Function class.
        This needs to be here, because Instance can have __init__ functions,
        which act the same way as normal functions.
        """
        return param.get_params(self._evaluator, self.base, self.var_args)

    def get_defined_names(self):
        """
        Call the default method with the own instance (self implements all
        the necessary functions). Add also the params.
        """
        return self._get_params() + pr.Scope.get_defined_names(self)

    def scope_names_generator(self, position=None):
        names = pr.filter_after_position(pr.Scope.get_defined_names(self), position)
        yield self, self._get_params() + names

    def _copy_properties(self, prop):
        """
        Literally copies a property of a Function. Copying is very expensive,
        because it is something like `copy.deepcopy`. However, these copied
        objects can be used for the executions, as if they were in the
        execution.
        """
        # Copy all these lists into this local function.
        attr = getattr(self.base, prop)
        objects = []
        for element in attr:
            if element is None:
                copied = element
            else:
                copied = helpers.fast_parent_copy(element)
                copied.parent = self._scope_copy(copied.parent)
                # TODO remove? Doesn't make sense, at least explain.
                if isinstance(copied, pr.Function):
                    copied = Function(self._evaluator, copied)
            objects.append(copied)
        return objects

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'imports', '_sub_module']:
            raise AttributeError('Tried to access %s: %s. Why?' % (name, self))
        return getattr(self.base, name)

    @memoize_default()
    def _scope_copy(self, scope):
        """ Copies a scope (e.g. if) in an execution """
        # TODO method uses different scopes than the subscopes property.

        # just check the start_pos, sometimes it's difficult with closures
        # to compare the scopes directly.
        if scope.start_pos == self.start_pos:
            return self
        else:
            copied = helpers.fast_parent_copy(scope)
            copied.parent = self._scope_copy(copied.parent)
            return copied

    @common.safe_property
    @memoize_default([])
    def returns(self):
        return self._copy_properties('returns')

    @common.safe_property
    @memoize_default([])
    def asserts(self):
        return self._copy_properties('asserts')

    @common.safe_property
    @memoize_default([])
    def statements(self):
        return self._copy_properties('statements')

    @common.safe_property
    @memoize_default([])
    def subscopes(self):
        return self._copy_properties('subscopes')

    def get_statement_for_position(self, pos):
        return pr.Scope.get_statement_for_position(self, pos)

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self.base)


class ModuleWrapper(use_metaclass(CachedMetaClass, pr.Module, Wrapper)):
    def __init__(self, evaluator, module):
        self._evaluator = evaluator
        self._module = module

    def scope_names_generator(self, position=None):
        yield self, pr.filter_after_position(self._module.get_defined_names(), position)
        yield self, self._module_attributes()
        sub_modules = self._sub_modules()
        if sub_modules:
            yield self, self._sub_modules()

    @memoize_default()
    def _module_attributes(self):
        def parent_callback():
            return Instance(self._evaluator, compiled.create(self._evaluator, str))

        names = ['__file__', '__package__', '__doc__', '__name__', '__version__']
        # All the additional module attributes are strings.
        return [helpers.LazyName(n, parent_callback) for n in names]

    @memoize_default()
    def _sub_modules(self):
        """
        Lists modules in the directory of this module (if this module is a
        package).
        """
        path = self._module.path
        names = []
        if path is not None and path.endswith(os.path.sep + '__init__.py'):
            mods = pkgutil.iter_modules([os.path.dirname(path)])
            for module_loader, name, is_pkg in mods:
                name = helpers.FakeName(name)
                # It's obviously a relative import to the current module.
                imp = helpers.FakeImport(name, self, level=1)
                name.parent = imp
                names.append(name)
        return names

    def __getattr__(self, name):
        return getattr(self._module, name)

    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, self._module)
