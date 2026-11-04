"""
Spec for the address stored on authenticated audit rows.

``LogEntry.remote_addr`` is the address of the immediate peer
(``request.META['REMOTE_ADDR']``), unless that peer is listed in the
``AUDITLOG_TRUSTED_PROXIES`` setting. The setting is a sequence of IP
address strings. When it is missing or empty, no peer is trusted.

When the immediate peer is trusted, the recorded address is the rightmost
hop in ``X-Forwarded-For`` that is a valid IP address and is not itself
listed in ``AUDITLOG_TRUSTED_PROXIES``. Hops are split on commas and
stripped of surrounding whitespace. Empty tokens and tokens that are not
valid IP addresses are ignored. If no such hop remains, the immediate peer
is kept.

An address is valid when the validators installed on
``GenericIPAddressField`` accept it (IPv4 or IPv6). A port suffix, an
empty token, or any other string those validators reject is not a valid
address. Anything else is stored as NULL rather than written onto the row.

These tests fail while a client-supplied ``X-Forwarded-For`` value is
copied onto the row ahead of the peer address.

``QuerySet.update()`` and ``QuerySet.bulk_create()`` never send the save
signals auditlog listens for, so they do not write ``remote_addr``.
``ManyRelatedManager.add()`` inserts through rows with ``bulk_create`` and
is in that group. Those paths are not exercised here.
"""
from django.conf.urls import url
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase, override_settings

from auditlog.models import LogEntry
from auditlog_tests.models import SimpleModel


PEER = '203.0.113.10'
FORGED = '198.51.100.23'
CLIENT = '203.0.113.77'
PROXY_A = '10.0.0.1'
PROXY_B = '10.0.0.2'
IPV6_PEER = '2001:db8::10'
IPV6_FORGED = '2001:db8::23'


def audited_change(request):
    """Perform one registered-model mutation and return its primary key."""
    operation = request.GET.get('op')
    if operation == 'create':
        obj = SimpleModel.objects.create(text='created-from-request')
        return HttpResponse(str(obj.pk))
    if operation == 'update':
        obj = SimpleModel.objects.get(pk=request.GET['pk'])
        obj.text = 'updated-from-request'
        obj.save()
        return HttpResponse(str(obj.pk))
    if operation == 'delete':
        obj = SimpleModel.objects.get(pk=request.GET['pk'])
        pk = obj.pk
        obj.delete()
        return HttpResponse(str(pk))
    if operation == 'restamp':
        # Any LogEntry save during the request, not only rows the receivers
        # just created, is stamped by the same pre_save handler.
        entry = LogEntry.objects.get(pk=request.GET['pk'])
        entry.save()
        return HttpResponse(str(entry.pk))
    return HttpResponse(status=400)


urlpatterns = [
    url(r'^audited/$', audited_change),
]


@override_settings(ROOT_URLCONF='auditlog_tests.test_remote_addr')
class RemoteAddrTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model(username='auditor')
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)

    def _get(self, operation, remote_addr, forwarded_for=None, **params):
        extra = {'REMOTE_ADDR': remote_addr}
        if forwarded_for is not None:
            extra['HTTP_X_FORWARDED_FOR'] = forwarded_for
        query = {'op': operation}
        query.update(params)
        return self.client.get('/audited/', query, **extra)

    def _assert_row(self, entry, expected_addr, object_pk):
        # One row must carry the actor, the object that was changed, and the
        # address together. A fix that authorizes one object and stamps
        # another, or that writes the address only onto a different row,
        # fails here.
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertEqual(str(entry.object_pk), str(object_pk))
        self.assertEqual(entry.remote_addr, expected_addr)

    def test_create_records_peer_address(self):
        response = self._get('create', PEER, FORGED)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, PEER, obj.pk)

    def test_update_records_peer_address(self):
        obj = SimpleModel.objects.create(text='before')
        response = self._get('update', PEER, FORGED, pk=obj.pk)
        self.assertEqual(response.status_code, 200)
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.UPDATE)
        self.assertIn('text', entry.changes_dict)
        self._assert_row(entry, PEER, obj.pk)

    def test_delete_records_peer_address(self):
        obj = SimpleModel.objects.create(text='before')
        pk = obj.pk
        response = self._get('delete', PEER, FORGED, pk=pk)
        self.assertEqual(response.status_code, 200)
        entry = LogEntry.objects.get(object_id=pk, action=LogEntry.Action.DELETE)
        self._assert_row(entry, PEER, pk)

    def test_resave_of_existing_logentry_records_peer_address(self):
        obj = SimpleModel.objects.create(text='before')
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self.assertIsNone(entry.remote_addr)
        response = self._get('restamp', PEER, FORGED, pk=entry.pk)
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self._assert_row(entry, PEER, obj.pk)

    def test_untrusted_peer_ignores_forwarded_chain(self):
        # The first hop is the forged address. A later hop repeats a proxy
        # address. The peer itself is not trusted, so none of the header is used.
        forwarded = '%s, %s, %s' % (FORGED, CLIENT, PROXY_A)
        response = self._get('create', PEER, forwarded)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, PEER, obj.pk)

    def test_untrusted_peer_ignores_ipv6_forwarded_for(self):
        response = self._get('create', IPV6_PEER, IPV6_FORGED)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, IPV6_PEER, obj.pk)

    def test_untrusted_peer_ignores_padded_first_hop(self):
        response = self._get('create', PEER, '  %s  , %s' % (FORGED, CLIENT))
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, PEER, obj.pk)

    def test_invalid_forwarded_for_does_not_replace_peer(self):
        for header in ('not-an-ip', '203.0.113.77:443', '999.999.999.999', ' , '):
            LogEntry.objects.all().delete()
            SimpleModel.objects.all().delete()
            response = self._get('create', PEER, header)
            self.assertEqual(response.status_code, 200, header)
            obj = SimpleModel.objects.get()
            entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
            self._assert_row(entry, PEER, obj.pk)

    def test_missing_header_records_peer(self):
        response = self._get('create', PEER)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, PEER, obj.pk)

    def test_invalid_peer_is_not_stored(self):
        response = self._get('create', 'not-a-peer')
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertIsNone(entry.remote_addr)

    @override_settings(AUDITLOG_TRUSTED_PROXIES=[PROXY_A, PROXY_B])
    def test_trusted_proxy_uses_rightmost_untrusted_hop(self):
        # Attacker-chosen address is the first hop. The trusted proxy appended
        # the client it actually saw, then its own address.
        forwarded = '%s, %s, %s' % (FORGED, CLIENT, PROXY_B)
        response = self._get('create', PROXY_A, forwarded)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, CLIENT, obj.pk)

    @override_settings(AUDITLOG_TRUSTED_PROXIES=[PROXY_A])
    def test_trusted_proxy_skips_invalid_hops(self):
        forwarded = 'not-an-ip, %s:443, %s' % (FORGED, CLIENT)
        response = self._get('update', PROXY_A, forwarded, pk=SimpleModel.objects.create(text='before').pk)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get()
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.UPDATE)
        self._assert_row(entry, CLIENT, obj.pk)

    @override_settings(AUDITLOG_TRUSTED_PROXIES=[PROXY_A, PROXY_B])
    def test_trusted_proxy_without_untrusted_hop_keeps_peer(self):
        forwarded = 'not-an-ip, %s' % PROXY_B
        response = self._get('create', PROXY_A, forwarded)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, PROXY_A, obj.pk)

    @override_settings(AUDITLOG_TRUSTED_PROXIES=[PROXY_A])
    def test_trusted_proxy_ipv6_client_is_rightmost_untrusted_hop(self):
        forwarded = '%s,%s' % (FORGED, IPV6_PEER)
        response = self._get('create', PROXY_A, forwarded)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self._assert_row(entry, IPV6_PEER, obj.pk)

    def test_unauthenticated_change_does_not_store_forwarded_for(self):
        self.client.logout()
        response = self._get('create', PEER, FORGED)
        self.assertEqual(response.status_code, 200)
        obj = SimpleModel.objects.get(pk=response.content.decode())
        entry = LogEntry.objects.get_for_object(obj).get(action=LogEntry.Action.CREATE)
        self.assertIsNone(entry.actor_id)
        self.assertIsNone(entry.remote_addr)
