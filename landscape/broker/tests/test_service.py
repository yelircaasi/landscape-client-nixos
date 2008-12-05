from dbus.service import signal

from twisted.internet.defer import Deferred

from landscape.schema import Message
from landscape.broker.broker import IFACE_NAME
from landscape.tests.helpers import (
    LandscapeIsolatedTest, RemoteBrokerHelper, LandscapeTest)
from landscape.lib.bpickle import dumps, loads
from landscape.lib.dbus_util import (Object, method,
                                     byte_array, array_to_string)
from landscape.lib.twisted_util import gather_results
from landscape.manager.manager import FAILED
from landscape.tests.helpers import DEFAULT_ACCEPTED_TYPES


class SampleSignalReceiver(object):

    def __init__(self, remote_exchange, bus):
        self.bus = bus
        self.signal_waiters = {}

    def got_signal(self, name, *data):
        if name in self.signal_waiters:
            self.signal_waiters[name].callback(data)

    def wait_for_signal(self, name):
        handler = lambda *args: self.got_signal(name, *args)
        self.bus.add_signal_receiver(handler, name)
        self.signal_waiters[name] = Deferred()
        return self.signal_waiters[name]



class BrokerDBusObjectTest(LandscapeIsolatedTest):

    helpers = [RemoteBrokerHelper]

    def setUp(self):
        super(BrokerDBusObjectTest, self).setUp()
        self.broker_service.message_store.set_accepted_types(["test"])
        self.broker_service.message_store.add_schema(Message("test", {}))

    def test_ping(self):
        """
        The broker can be pinged over DBUS to see if it is alive.
        """
        return self.remote_service.ping().addCallback(self.assertEquals, True)

    def test_send_message(self):
        """
        The L{BrokerDBusObject} should expose a remote
        C{send_message} method which adds a given message to the
        message store.
        """
        result = self.remote_service.send_message(
            byte_array(dumps({"type": "test"})), dbus_interface=IFACE_NAME)
        def got_result(message_id):
            service = self.broker_service
            self.assertTrue(service.message_store.is_pending(message_id))
            messages = service.message_store.get_pending_messages()
            self.assertEquals(len(messages), 1)
            self.assertMessage(messages[0], {"type": "test"})
        result.addCallback(got_result)
        return result

    def test_send_urgent_message(self):
        """
        The C{send_message} method should take a flag indicating that
        the client should be put into urgent exchange mode.
        """
        result = self.remote_service.send_message(
            byte_array(dumps({"type": "test"})), True,
            dbus_interface=IFACE_NAME)
        def got_result(result):
            messages = self.broker_service.message_store.get_pending_messages()
            self.assertEquals(len(messages), 1)
            self.assertMessage(messages[0], {"type": "test"})
            self.assertTrue(self.broker_service.exchanger.is_urgent())
        result.addCallback(got_result)
        return result

    def test_is_message_pending_true(self):
        """
        The L{BrokerDBusObject} should expose a remote
        C{send_message} method which adds a given message to the
        message store.
        """
        message_id = self.broker_service.message_store.add({"type": "test"})
        result = self.remote_service.is_message_pending(message_id)
        def got_result(is_pending):
            self.assertEquals(is_pending, True)
        return result.addCallback(got_result)

    def test_is_message_pending_false(self):
        """
        The L{BrokerDBusObject} should expose a remote
        C{send_message} method which adds a given message to the
        message store.
        """
        message_id = self.broker_service.message_store.add({"type": "test"})
        self.broker_service.message_store.add_pending_offset(1)
        result = self.remote_service.is_message_pending(message_id)
        def got_result(is_pending):
            self.assertEquals(is_pending, False)
        return result.addCallback(got_result)

    def test_exchange_notification(self):
        """
        The BrokerDBusObject will broadcast a C{impending_exchange} signal
        before exchanging, to give plugins a chance to send messages to get
        into the next exchange. It does this by hooking in to the
        C{impending-exchange} event.
        """
        plugin_service = SampleSignalReceiver(self.remote_service,
                                              self.broker_service.bus)
        result = plugin_service.wait_for_signal("impending_exchange")
        self.broker_service.reactor.fire("impending-exchange")
        # The typical failure case for this test is to hang until timeout :\
        return result
    test_exchange_notification.timeout = 4

    def test_exchange_failed_notification(self):
        """
        The BrokerService will broadcast a C{exchange_failed} signal
        if the exchange fails.
        """
        plugin_service = SampleSignalReceiver(self.remote_service,
                                              self.broker_service.bus)
        result = plugin_service.wait_for_signal("exchange_failed")
        self.broker_service.reactor.fire("exchange-failed")
        # The typical failure case for this test is to hang until timeout :\
        return result
    test_exchange_failed_notification.timeout = 4

    def test_resynchronize_clients(self):
        """
        The exchange broadcasts the reactor event 'resynchronize-clients'; in
        this case the BrokerDBusObject should broadcast a dbus signal
        'resynchronize'.
        """
        plugin_service = SampleSignalReceiver(self.remote_service,
                                              self.broker_service.bus)
        result = plugin_service.wait_for_signal("resynchronize")
        self.broker_service.reactor.fire("resynchronize-clients")
        # The typical failure case for this test is to hang until timeout :\
        return result
    test_resynchronize_clients.timeout = 4

    def test_broadcast_messages(self):
        """
        The DBus service calls the 'message' method on all registered plugins
        when messages are received from the server. The message is passed as a
        bpickle.
        """

        final_message = Deferred()
        class MyService(Object):
            bus_name = "my.service.name"
            object_path = "/my/service/name"
            @method(bus_name)
            def message(self, message):
                final_message.callback(message)

        my_service = MyService(self.broker_service.bus)

        registration = self.remote.register_plugin(
            "my.service.name", "/my/service/name")

        def registered(result):
            transport = self.broker_service.transport
            transport.responses.append([{"type": "foobar", "value": 42}])
            self.broker_service.exchanger.exchange()
        registration.addCallback(registered)

        def ready(message):
            message = array_to_string(message)
            message = loads(message)
            self.assertEquals(message,
                              {"type": "foobar", "value": 42})

        final_message.addCallback(ready)
        return final_message

    test_broadcast_messages.timeout = 4

    def test_failed_operation_without_plugins(self):
        """
        When there are no broker plugins available to handle a message, an
        operation-result message should be sent back to the server indicating a
        failure.
        """
        self.log_helper.ignore_errors("Nobody handled the foobar message.")
        self.broker_service.message_store.set_accepted_types(
            ["operation-result"])
        result = self.broker_service.reactor.fire("message",
                                                  {"type": "foobar",
                                                   "operation-id": 4})
        result = [result for result in result if result is not None][0]
        class Startswith(object):
            def __eq__(self, other):
                return other.startswith(
                    "Landscape client failed to handle this request (foobar)")
        def broadcasted(ignored):
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [{"type": "operation-result",
                  "status": FAILED,
                  "result-text": Startswith(),
                  "operation-id": 4}])
        result.addCallback(broadcasted)
        return result


    def test_failed_operation_with_plugins_not_handling(self):
        """
        When no broker plugins handle a message (i.e., they return False from
        the message() call), an operation-result message should be sent back to
        the server indicating a failure.
        """
        self.log_helper.ignore_errors("Nobody handled the foobar message.")
        class MyService(Object):
            bus_name = "my.service.name"
            object_path = "/my/service/name"
            @method(bus_name)
            def message(self, message):
                self.called = True
                return False

        self.broker_service.message_store.set_accepted_types(
            ["operation-result"])

        my_service = MyService(self.broker_service.bus)

        result = self.remote.register_plugin(
            "my.service.name", "/my/service/name")
        def registered(ignored):
            result = self.broker_service.reactor.fire("message",
                                                      {"type": "foobar",
                                                       "operation-id": 4})
            return [result for result in result if result is not None][0]

        class Startswith(object):
            def __eq__(self, other):
                return other.startswith(
                    "Landscape client failed to handle this request (foobar)")
        def broadcasted(ignored):
            self.assertTrue(my_service.called)
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [{"type": "operation-result",
                  "status": FAILED,
                  "result-text": Startswith(),
                  "operation-id": 4}])
        result.addCallback(registered)
        result.addCallback(broadcasted)
        return result


    def test_resynchronize_not_handled_by_plugins(self):
        """
        *resynchronize* operations are special, in that we know the broker
        handles them in a special way. If none of the broker-plugins respond
        to a resynchronize event, we should not send back a failure, because
        the broker itself will respond to those.
        """
        self.broker_service.message_store.set_accepted_types(
            ["operation-result"])
        result = self.broker_service.reactor.fire("message",
                                                  {"type": "resynchronize",
                                                   "operation-id": 4})
        result = [result for result in result if result is not None][0]
        def broadcasted(ignored):
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [])
        result.addCallback(broadcasted)
        return result


    def test_register(self):
        """
        Remote parties can request a registration to be made with the server.
        """
        identity = self.broker_service.identity

        def register_done(deferred_result):
            self.assertEquals(deferred_result.called, False)

            self.broker_service.exchanger.handle_message(
                {"type": "set-id", "id": "SECURE",
                 "insecure-id": "INSECURE"})

            self.assertEquals(deferred_result.called, True)

        # Hook register_done() to be called after register() returns.  We
        # must only fire the "set-id" message after this method returns,
        # since that's when the deferred is created and hooked on the
        # related events.
        registration_mock = self.mocker.patch(self.broker_service.registration)
        registration_mock.register()
        self.mocker.passthrough(register_done)
        self.mocker.replay()

        return self.remote_service.register()

    def test_registration_done_event_becomes_signal(self):
        waiter = Deferred()
        def got_signal():
            waiter.callback("We got it!")
        self.broker_service.bus.add_signal_receiver(got_signal,
                                                    "registration_done")
        self.broker_service.reactor.fire("registration-done")
        return waiter

    def test_registration_failed_event_becomes_signal(self):
        waiter = Deferred()
        def got_signal():
            waiter.callback("We got it!")
        self.broker_service.bus.add_signal_receiver(got_signal,
                                                    "registration_failed")
        self.broker_service.reactor.fire("registration-failed")
        return waiter

    def test_reload_configuration(self):
        open(self.config_filename, "a").write("computer_title = New Title")
        result = self.remote_service.reload_configuration()
        def got_result(result):
            self.assertEquals(self.broker_service.config.computer_title,
                              "New Title")
        return result.addCallback(got_result)

    def test_reload_configuration_stops_plugins(self):
        """
        Reloading the configuration must stop all clients (by calling C{exit}
        on them) so that they can be restarted by the watchdog and see the new
        changes in the config file.
        """
        class MyService(Object):
            bus_name = "my.service.name"
            object_path = "/my/service/name"
            def __init__(self, *args, **kw):
                Object.__init__(self, *args, **kw)
                self.stash = []

            @method(bus_name)
            def exit(self):
                self.stash.append(True)
        my_service = MyService(self.broker_service.bus)
        def got_result(result):
            self.assertEquals(my_service.stash, [True])
        self.remote.register_plugin("my.service.name", "/my/service/name")
        result = self.remote.reload_configuration()
        return result.addCallback(got_result)

    def test_get_accepted_types_empty(self):
        self.broker_service.message_store.set_accepted_types([])
        deferred = self.remote_service.get_accepted_message_types()
        def got_result(result):
            self.assertEquals(result, [])
        return deferred.addCallback(got_result)

    def test_get_accepted_message_types(self):
        self.broker_service.message_store.set_accepted_types(["foo", "bar"])
        deferred = self.remote_service.get_accepted_message_types()
        def got_result(result):
            self.assertEquals(set(result), set(["foo", "bar"]))
        return deferred.addCallback(got_result)

    def test_message_type_acceptance_changed_event_becomes_signal(self):
        waiter = Deferred()
        def got_signal(type, accepted):
            waiter.callback("We got it!")
            self.assertEquals(type, "some-type")
            self.assertEquals(accepted, True)

        self.broker_service.bus.add_signal_receiver(
                                         got_signal,
                                         "message_type_acceptance_changed")
        self.broker_service.reactor.fire("message-type-acceptance-changed",
                                         "some-type", True)
        return waiter

    def test_register_and_get_plugins(self):
        result = self.remote.register_plugin("service.name", "/Path")
        def got_result(result):
            result = self.remote.get_registered_plugins()
            result.addCallback(self.assertEquals, [("service.name", "/Path")])
            return result
        result.addCallback(got_result)
        return result

    def test_no_duplicate_plugins(self):
        """
        Adding the same plugin data twice does not cause duplicate entries.
        """
        result = self.remote.register_plugin("service.name", "/Path")
        result.addCallback(lambda ign: self.remote.register_plugin(
                "service.name", "/Path"))
        result.addCallback(lambda ign: self.remote.get_registered_plugins())
        result.addCallback(self.assertEquals, [("service.name", "/Path")])
        return result

    def test_exit(self):
        stash = []
        class MyService(Object):
            bus_name = "my.service.name"
            object_path = "/my/service/name"
            @method(bus_name)
            def exit(self):
                # We'll actually change the stash in a bit instead of right
                # now.  The idea is that the broker's exit method should wait
                # for us to do our whole thing before it returns.
                from twisted.internet import reactor
                deferred = Deferred()
                def change_stash():
                    stash.append(True)
                    deferred.callback(None)
                reactor.callLater(0.2, change_stash)
                return deferred
        self.my_service = MyService(self.broker_service.bus)
        def got_result(result):
            self.assertEquals(stash, [True])
        self.remote.register_plugin("my.service.name", "/my/service/name")
        result = self.remote.exit()
        return result.addCallback(got_result)

    def test_exit_runs_quickly_with_missing_services(self):
        """
        If other daemons die, the Broker won't retry them for ages.
        """
        self.log_helper.ignore_errors(ZeroDivisionError)

        self.remote.register_plugin("my.service.name", "/my/service/name")

        post_exits = []
        self.broker_service.reactor.call_on("post-exit",
                                            lambda: post_exits.append(True))

        def took_too_long():
            result.errback(Exception("It took too long!"))

        def cancel_delayed(result):
            delayed.cancel()

        from twisted.internet import reactor
        delayed = reactor.callLater(5, took_too_long)

        result = self.remote.exit()
        result.addCallback(cancel_delayed)
        return result

    def test_exit_exits_when_other_daemons_blow_up(self):
        """
        If other daemons blow up in their exit() methods, exit should ignore
        the error and exit anyway.
        """
        self.log_helper.ignore_errors(ZeroDivisionError)

        class MyService(Object):
            bus_name = "my.service.name"
            object_path = "/my/service/name"
            @method(bus_name)
            def exit(self):
                1/0
        self.my_service = MyService(self.broker_service.bus)
        self.remote.register_plugin("my.service.name", "/my/service/name")

        post_exits = []
        self.broker_service.reactor.call_on("post-exit",
                                            lambda: post_exits.append(True))

        def got_result(result):
            # The actual exit happens a second after the dbus response.
            self.broker_service.reactor.advance(1)
            self.assertEquals(post_exits, [True])

        result = self.remote.exit()
        return result.addCallback(got_result)

    def test_exit_fires_reactor_events(self):
        stash = []

        self.broker_service.reactor.call_on("pre-exit",
                                            lambda: stash.append("pre"))
        self.broker_service.reactor.call_on("post-exit",
                                            lambda: stash.append("post"))

        def got_result(result):
            self.broker_service.reactor.advance(1)
            self.assertEquals(stash, ["pre", "post"])

        result = self.remote.exit()
        result.addCallback(got_result)
        return result

    def test_call_if_accepted(self):
        """
        If a plugins message type is accepted, call a given function.
        """
        self.broker_service.message_store.set_accepted_types(["foo"])
        l = []

        deferred = self.remote.call_if_accepted("foo", l.append, True)
        def got_accepted(result):
            self.assertEquals(l, [True])

        deferred.addCallback(got_accepted)
        return deferred

    def test_not_called_if_not_accepted(self):
        """
        If a plugins message type is not accepted, don't call a given
        function.
        """
        l = []

        deferred = self.remote.call_if_accepted("foo", l.append, True)
        def got_accepted(result):
            self.assertEquals(l, [])
            
        deferred.addCallback(got_accepted)
        return deferred

    def test_value_of_called_if_accepted(self):
        """
        If a plugins message type is not accepted, don't call a given
        function.
        """
        self.broker_service.message_store.set_accepted_types(["foo"])
        deferred = self.remote.call_if_accepted("foo", lambda: "hi")
        def got_accepted(result):
            self.assertEquals(result, "hi")

        deferred.addCallback(got_accepted)
        return deferred

    def test_register_accepted_message_type(self):
        result1 = self.remote.register_client_accepted_message_type("type1")
        result2 = self.remote.register_client_accepted_message_type("type2")
        def got_result(result):
            exchanger = self.broker_service.exchanger
            types = exchanger.get_client_accepted_message_types()
            self.assertEquals(
                types,
                sorted(["type1", "type2"] + DEFAULT_ACCEPTED_TYPES))
        return gather_results([result1, result2]).addCallback(got_result)
        

