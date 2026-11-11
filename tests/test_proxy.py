from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from server import forwarded, local_proxy, proxy_networks, same_origin

TAILNET = 'host.example.ts.net:8443'
FORWARDED = {'Host': TAILNET, 'Origin': 'https://'+TAILNET,
             'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '100.64.0.7'}


async def echo(request):
    return web.json_response(dict(scheme=request.scheme, remote=request.remote))


class ProxyTests(IsolatedAsyncioTestCase):
    async def test_tailscale_serve_join_passes_origin_check(self):
        app = web.Application(middlewares=[local_proxy, same_origin])
        app.router.add_post('/api/join', echo)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/join', headers=FORWARDED)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), dict(scheme='https', remote='100.64.0.7'))
            response = await client.post('/api/join', headers={'Host': TAILNET, 'Origin': 'https://'+TAILNET})
            self.assertEqual(response.status, 403, 'without the proxy header the scheme stays http')

    async def test_forwarded_headers_ignored_from_lan_peer(self):
        transport = Mock()
        transport.get_extra_info.side_effect = lambda key, default=None: ('192.168.1.20', 5000) if key == 'peername' else default
        request = make_mocked_request('POST', '/api/join', headers=FORWARDED, transport=transport)
        seen = await local_proxy(request, lambda r: _async(r))
        self.assertEqual((seen.scheme, seen.remote), ('http', '192.168.1.20'))


def peer(address):
    transport = Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: (address, 5000) if key == 'peername' else default
    return transport


SITE = 'play.example.org'
LAN_PROXY = {'Host': SITE, 'Origin': 'https://'+SITE, 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.9'}


class TrustedProxyTests(IsolatedAsyncioTestCase):
    async def seen(self, address, headers, networks=proxy_networks('192.0.2.0/24')):
        request = make_mocked_request('POST', '/api/join', headers=headers, transport=peer(address))
        return await forwarded(networks)(request, _async)

    async def test_https_origin_from_a_trusted_lan_proxy_passes(self):
        seen = await self.seen('192.0.2.5', LAN_PROXY)
        self.assertEqual((seen.scheme, seen.host, seen.remote), ('https', SITE, '203.0.113.9'))
        await same_origin(seen, lambda request: _async(web.Response()))
        seen = await self.seen('::ffff:192.0.2.5', LAN_PROXY)
        self.assertEqual(seen.scheme, 'https', 'an IPv4 peer on a dual-stack socket')

    async def test_forged_headers_from_an_untrusted_peer_are_ignored(self):
        seen = await self.seen('198.51.100.7', dict(LAN_PROXY, **{'X-Forwarded-Host': 'other.example'}))
        self.assertEqual((seen.scheme, seen.host, seen.remote), ('http', SITE, '198.51.100.7'))
        with self.assertRaises(web.HTTPForbidden):
            await same_origin(seen, lambda request: _async(web.Response()))

    async def test_default_trusts_loopback_only(self):
        seen = await self.seen('192.0.2.5', LAN_PROXY, ())
        self.assertEqual(seen.scheme, 'http')
        seen = await self.seen('127.0.0.1', LAN_PROXY, ())
        self.assertEqual(seen.scheme, 'https')

    async def test_a_proxy_that_rewrites_host_sends_x_forwarded_host(self):
        headers = dict(LAN_PROXY, Host='10.0.0.2:8081', **{'X-Forwarded-Host': SITE})
        seen = await self.seen('192.0.2.5', headers)
        self.assertEqual(seen.host, SITE)
        await same_origin(seen, lambda request: _async(web.Response()))

    async def test_cross_origin_is_still_refused_through_a_trusted_proxy(self):
        seen = await self.seen('192.0.2.5', dict(LAN_PROXY, Origin='https://evil.example'))
        with self.assertRaises(web.HTTPForbidden):
            await same_origin(seen, lambda request: _async(web.Response()))

    def test_list_parsing(self):
        self.assertEqual([str(n) for n in proxy_networks('192.0.2.5, 198.51.100.0/24,')],
                         ['192.0.2.5/32', '198.51.100.0/24'])
        with self.assertRaises(Exception):
            proxy_networks('proxy.example')


async def _async(value):
    return value
