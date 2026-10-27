from fastapi.testclient import TestClient
from tests.web.test_cloud_auth import cloud_app

TOOLS = {'workspace_search', 'page_get', 'page_create', 'page_update_properties', 'page_edit_blocks',
         'page_get_history', 'document_get', 'unit_read', 'evidence_create', 'evidence_get'}


def rpc(client, token, method, params=None, number=1):
    return client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                       json={'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}})


def test_real_http_handshake_tools_scopes_and_revocation(cloud_app):
    auth = cloud_app.state.cloud_auth
    edit = auth.issue_pat('fixture-edit', 'edit')
    read = auth.issue_pat('fixture-read', 'read')
    with TestClient(cloud_app, base_url='https://lit.example.test') as client:
        assert client.post('/mcp').status_code == 401
        init = rpc(client, edit['token'], 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'acceptance', 'version': '1'}})
        assert init.status_code == 200, init.text
        assert 'serverInfo' in init.json()['result']
        listed = rpc(client, read['token'], 'tools/list').json()['result']['tools']
        assert {tool['name'] for tool in listed} == TOOLS
        request = {'name': 'page_create', 'arguments': {'title': 'From MCP', 'request_id': 'mcp-request'}}
        rejected = rpc(client, read['token'], 'tools/call', request).json()['result']
        assert rejected['isError']
        created = rpc(client, edit['token'], 'tools/call', request).json()['result']
        assert not created.get('isError'), created
        auth.revoke_pat(edit['id'])
        assert rpc(client, edit['token'], 'tools/list').status_code == 401
