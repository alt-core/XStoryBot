import hub


def load_plugin(params):
    from plugin.webchat.interface import WebchatInterfaceFactory
    hub.register_interface_factory(
        type_name='webchat',
        factory=WebchatInterfaceFactory(params),
    )
