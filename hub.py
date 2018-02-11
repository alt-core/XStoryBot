# coding: utf-8


builder_list = []
runtime_list = []

interface_factory_map = {}
scenario_loader_factory_map = {}

method_cache = {}


def clear():
    del builder_list[:]
    del runtime_list[:]
    interface_factory_map.clear()
    scenario_loader_factory_map.clear()
    method_cache.clear()


def register_handler(service, builder=None, runtime=None):
    if builder is not None:
        builder_list.append((service, builder))
    if runtime is not None:
        runtime_list.append((service, runtime))


def register_interface_factory(type_name, factory):
    interface_factory_map[type_name] = factory


def register_scenario_loader_factory(type_name, factory):
    scenario_loader_factory_map[type_name] = factory


def _get_method_list(method_list, kind, service, method_name):
    result = method_cache.get(service+":"+kind+":"+method_name)
    if result is None:
        result = []
        for s, m in method_list:
            if service == '*' or s == '*' or s == service:
                if hasattr(m, method_name):
                    result.append(getattr(m, method_name))
        method_cache[service+":"+kind+":"+method_name] = result
    return result


def _invoke_method(method_list, kind, service, method_name, *args):
    methods = _get_method_list(method_list, kind, service, method_name)
    if methods:
        # 最初に見つかったメソッドだけを呼び出す
        return methods[0](*args)
    # どのプラグインでもハンドルされなかった
    return None


def invoke_builder_method(method_name, *args):
    return _invoke_method(builder_list, "builder", '*', method_name, *args)


def invoke_runtime_method(method_name, context, *args):
    return _invoke_method(runtime_list, "runtime", context.service_name, method_name, context, *args)


def _invoke_all_methods(method_list, kind, service, method_name, *args):
    for method in _get_method_list(method_list, kind, service, method_name):
        if method(*args):
            # True が帰ってきたら終了する
            return True
    # どのプラグインでもハンドルされなかった
    return False


def invoke_all_builder_methods(method_name, *args):
    return _invoke_all_methods(builder_list, "builder", '*', method_name, *args)


def invoke_all_runtime_methods(method_name, context, *args):
    return _invoke_all_methods(runtime_list, "runtime", context.service_name, method_name, context, *args)


def _filter_all_methods(method_list, kind, service, method_name, builder_or_context, value, *args):
    for method in _get_method_list(method_list, kind, service, method_name):
        value = method(builder_or_context, value, *args)
        if value is None:
            return None
    return value


def filter_all_builder_methods(method_name, builder, value, *args):
    return _filter_all_methods(builder_list, "builder", '*', method_name, builder, value, *args)


def filter_all_runtime_methods(method_name, context, value, *args):
    return _filter_all_methods(runtime_list, "runtime", context.service_name, method_name, context, value, *args)


def create_interface(type_name, bot_name, params):
    factory = interface_factory_map.get(type_name, None)
    if factory:
        return factory.create_interface(bot_name, params)
    return None


def create_scenario_loader(type_name, params):
    factory = scenario_loader_factory_map.get(type_name, None)
    if factory:
        return factory.create_loader(params)
    return None
