"""Exercise the installed SDK's issuer boundary without network or real credentials."""
from pathlib import Path
import shutil
import subprocess


def test_sdk_credentials_stay_bound_to_their_authorization_server():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which('node')
    assert node, 'Node is required for the locked MCP Apps dependency checks'
    result = subprocess.run([node, '--input-type=module', '-e', '''
import assert from 'node:assert/strict';
import { fetchToken, AuthorizationServerMismatchError } from '@modelcontextprotocol/client';
import { OAuthClientInformationSchema } from '@modelcontextprotocol/core';

const trusted = 'https://trusted.fixture';
const credentials = {client_id: 'fixture-client', client_secret: 'fixture-only', issuer: trusted};
const stored = OAuthClientInformationSchema.parse(credentials);
assert.deepEqual(stored, credentials); // persistence must retain the issuer binding
let requests = [];
const provider = {
  clientMetadata: {redirect_uris: [], token_endpoint_auth_method: 'client_secret_post'},
  clientInformation: async () => stored,
  prepareTokenRequest: async () => new URLSearchParams({grant_type: 'client_credentials'})
};
const fetchFn = async (url, options) => {
  requests.push({url: String(url), body: String(options.body), headers: options.headers});
  return new Response(JSON.stringify({access_token: 'fixture-token', token_type: 'Bearer'}),
    {status: 200, headers: {'content-type': 'application/json'}});
};
for (const metadata of [undefined, {
  issuer: 'https://attacker.fixture', token_endpoint: 'https://attacker.fixture/token',
  token_endpoint_auth_methods_supported: ['client_secret_post']
}]) {
  await assert.rejects(fetchToken(provider, 'https://attacker.fixture', {metadata, fetchFn}),
    AuthorizationServerMismatchError);
  assert.equal(requests.length, 0); // no credential-bearing request, including fallback discovery
}
const token = await fetchToken(provider, trusted, {fetchFn, metadata: {
  issuer: trusted, token_endpoint: trusted + '/token',
  token_endpoint_auth_methods_supported: ['client_secret_post']
}});
assert.equal(token.access_token, 'fixture-token');
assert.equal(requests.length, 1);
assert.equal(requests[0].url, trusted + '/token');
assert.equal(new URLSearchParams(requests[0].body).get('client_secret'), credentials.client_secret);
'''], cwd=root/'web/mcp-apps', capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
