from __future__ import unicode_literals

import threading
import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db.models.signals import pre_save
from django.utils import six
from django.utils.functional import curry
from django.utils.ipv6 import clean_ipv6_address
from django.apps import apps
from auditlog.models import LogEntry
from auditlog.compat import is_authenticated

# Use MiddlewareMixin when present (Django >= 1.10)
try:
    from django.utils.deprecation import MiddlewareMixin
except ImportError:
    MiddlewareMixin = object


threadlocal = threading.local()


def _clean_ip_address(value):
    """
    Return ``value`` as a normalized IP address string, or None if it is not an address ``LogEntry.remote_addr``
    accepts.
    """
    if not isinstance(value, six.string_types):
        return None
    value = value.strip()
    try:
        validate_ipv46_address(value)
    except ValidationError:
        return None
    if ':' in value:
        return clean_ipv6_address(value)
    return value


def get_client_address(request):
    """
    Return the address of the client that made ``request``.

    This is ``REMOTE_ADDR`` unless that peer is listed in the ``AUDITLOG_TRUSTED_PROXIES`` setting. For a trusted
    peer, ``X-Forwarded-For`` is read from the right, skipping hops that are also trusted proxies, and the first
    untrusted hop is used. Hops further left were supplied by the client and are never used. If a hop that is not a
    valid IP address is reached first, the chain can no longer be relied on and the peer address is kept.
    """
    peer = _clean_ip_address(request.META.get('REMOTE_ADDR'))
    trusted_proxies = set(
        address for address in map(_clean_ip_address, getattr(settings, 'AUDITLOG_TRUSTED_PROXIES', None) or ())
        if address is not None
    )
    if peer is None or peer not in trusted_proxies:
        return peer

    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR') or ''
    for hop in reversed(forwarded_for.split(',')):
        address = _clean_ip_address(hop)
        if address is None:
            break
        if address not in trusted_proxies:
            return address
    return peer


class AuditlogMiddleware(MiddlewareMixin):
    """
    Middleware to couple the request's user to log items. This is accomplished by currying the signal receiver with the
    user from the request (or None if the user is not authenticated).
    """

    def process_request(self, request):
        """
        Gets the current user from the request and prepares and connects a signal receiver with the user already
        attached to it.
        """
        # Initialize thread local storage
        threadlocal.auditlog = {
            'signal_duid': (self.__class__, time.time()),
            'remote_addr': get_client_address(request),
        }

        # Connect signal for automatic logging
        if hasattr(request, 'user') and is_authenticated(request.user):
            set_actor = curry(self.set_actor, user=request.user, signal_duid=threadlocal.auditlog['signal_duid'])
            pre_save.connect(set_actor, sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'], weak=False)

    def process_response(self, request, response):
        """
        Disconnects the signal receiver to prevent it from staying active.
        """
        if hasattr(threadlocal, 'auditlog'):
            pre_save.disconnect(sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'])

        return response

    def process_exception(self, request, exception):
        """
        Disconnects the signal receiver to prevent it from staying active in case of an exception.
        """
        if hasattr(threadlocal, 'auditlog'):
            pre_save.disconnect(sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'])

        return None

    @staticmethod
    def set_actor(user, sender, instance, signal_duid, **kwargs):
        """
        Signal receiver with an extra, required 'user' kwarg. This method becomes a real (valid) signal receiver when
        it is curried with the actor.
        """
        if hasattr(threadlocal, 'auditlog'):
            if signal_duid != threadlocal.auditlog['signal_duid']:
                return
            try:
                app_label, model_name = settings.AUTH_USER_MODEL.split('.')
                auth_user_model = apps.get_model(app_label, model_name)
            except ValueError:
                auth_user_model = apps.get_model('auth', 'user')
            if sender == LogEntry and isinstance(user, auth_user_model) and instance.actor is None:
                instance.actor = user

            instance.remote_addr = threadlocal.auditlog['remote_addr']
