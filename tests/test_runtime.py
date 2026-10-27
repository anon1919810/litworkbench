import pytest

from litworkbench.runtime import RuntimeSettings


def cloud_env():
    return {
        'LIT_MODE': 'cloud', 'LIT_PUBLIC_URL': 'https://lit.example.test',
        'LIT_ALLOWED_HOSTS': 'lit.example.test',
        'LIT_OIDC_ISSUER': 'https://identity.example.test',
        'LIT_OIDC_CLIENT_ID': 'fixture', 'LIT_OIDC_CLIENT_SECRET': 'fixture-secret',
        'LIT_OWNER_SUBJECT': 'owner-sub', 'LIT_SESSION_SECRET': 's' * 48,
        'LIT_TOKEN_SECRET': 't' * 48,
    }


def test_default_is_local_and_cloud_fails_closed():
    assert RuntimeSettings.from_env({}).mode == 'local'
    with pytest.raises(ValueError):
        RuntimeSettings.from_env({'LIT_MODE': 'cloud'})
    with pytest.raises(ValueError):
        RuntimeSettings.from_env({'LIT_MODE': 'typo'})


@pytest.mark.parametrize('key,value', [
    ('LIT_PUBLIC_URL', 'http://lit.example.test'),
    ('LIT_PUBLIC_URL', 'https://lit.example.test/path'),
    ('LIT_PUBLIC_URL', 'https://user:secret@lit.example.test'),
    ('LIT_ALLOWED_HOSTS', '*'), ('LIT_SESSION_SECRET', 'short'),
    ('LIT_OIDC_ISSUER', 'http://identity.example.test'),
])
def test_invalid_cloud_configuration(key, value):
    env = cloud_env(); env[key] = value
    with pytest.raises(ValueError):
        RuntimeSettings.from_env(env)


def test_cloud_secrets_not_in_repr():
    settings = RuntimeSettings.from_env(cloud_env())
    assert 'fixture-secret' not in repr(settings)
    assert 's' * 48 not in repr(settings)
    assert settings.allowed_hosts == ('lit.example.test',)
