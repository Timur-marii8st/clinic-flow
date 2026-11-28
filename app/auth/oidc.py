"""Confidential OIDC client: authorization code + PKCE, no tokens in browser.

Only the configured HTTPS issuer and its same-origin discovery endpoints are
trusted. RS256 only for this initial Keycloak-compatible integration.
"""
from dataclasses import dataclass
import base64
import hashlib
import hmac
import os
from urllib.parse import urlencode, urlsplit

import httpx
import jwt


def https_url(value, origin_only=False):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (origin_only and parsed.path not in ('', '/'))):
        raise ValueError('Expected a configured HTTPS URL')
    return value.rstrip('/')


def origin(value):
    parsed = urlsplit(value)
    return (parsed.scheme, parsed.hostname, parsed.port or 443)


@dataclass(frozen=True)
class SecuritySettings:
    public_origin: str
    issuer: str
    client_id: str
    client_secret: str
    bootstrap_subject: str

    def __post_init__(self):
        object.__setattr__(self,'public_origin',https_url(self.public_origin, True))
        object.__setattr__(self,'issuer',https_url(self.issuer))
        if not all(isinstance(x,str) and x.strip() for x in (self.client_id,self.client_secret,self.bootstrap_subject)):
            raise ValueError('Secure mode requires OIDC client and bootstrap subject')

    @classmethod
    def from_env(cls):
        mode = os.environ.get('CLINIC_MODE','demo')
        if mode == 'demo':
            return None
        if mode != 'secure':
            raise ValueError('CLINIC_MODE must be demo or secure')
        return cls(*(os.environ.get(k,'') for k in ('CLINIC_PUBLIC_ORIGIN','CLINIC_OIDC_ISSUER',
                   'CLINIC_OIDC_CLIENT_ID','CLINIC_OIDC_CLIENT_SECRET','CLINIC_BOOTSTRAP_SUBJECT')))

    @property
    def callback_url(self):
        return self.public_origin + '/auth/callback'


class OIDCClient:
    def __init__(self, settings, transport=None):
        self.settings=settings
        self.transport=transport

    def _http(self):
        return httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False, transport=self.transport)

    async def _discovery(self, client):
        response=await client.get(self.settings.issuer+'/.well-known/openid-configuration')
        response.raise_for_status()
        metadata=response.json()
        if metadata['issuer'] != self.settings.issuer:
            raise ValueError('Issuer mismatch')
        for name in ('authorization_endpoint','token_endpoint','jwks_uri'):
            value=https_url(metadata[name])
            if origin(value)!=origin(self.settings.issuer):
                raise ValueError('Unexpected OIDC endpoint origin')
        if 'RS256' not in metadata.get('id_token_signing_alg_values_supported',[]):
            raise ValueError('Provider does not advertise RS256')
        if 'S256' not in metadata.get('code_challenge_methods_supported',[]):
            raise ValueError('Provider does not advertise PKCE S256')
        return metadata

    async def authorize(self, state, verifier, nonce):
        async with self._http() as client:
            metadata=await self._discovery(client)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params={'client_id':self.settings.client_id,'redirect_uri':self.settings.callback_url,
                'response_type':'code','scope':'openid','state':state,'nonce':nonce,'prompt':'login',
                'code_challenge':challenge,'code_challenge_method':'S256'}
        return metadata['authorization_endpoint']+'?'+urlencode(params)

    async def redeem(self, code, verifier, nonce):
        async with self._http() as client:
            metadata=await self._discovery(client)
            response=await client.post(metadata['token_endpoint'],
                data={'grant_type':'authorization_code','code':code,'redirect_uri':self.settings.callback_url,
                      'code_verifier':verifier},
                auth=(self.settings.client_id,self.settings.client_secret))
            response.raise_for_status()
            encoded=response.json()['id_token']
            header=jwt.get_unverified_header(encoded)
            if header.get('alg')!='RS256' or not isinstance(header.get('kid'),str):
                raise ValueError('Unexpected signing algorithm or key')
            response=await client.get(metadata['jwks_uri'])
            response.raise_for_status()
            keys=[k for k in response.json()['keys'] if k.get('kid')==header['kid'] and
                  k.get('kty')=='RSA' and k.get('use','sig')=='sig' and k.get('alg','RS256')=='RS256']
            if len(keys)!=1:
                raise ValueError('Unexpected signing key')
            key=jwt.PyJWK.from_dict(keys[0],algorithm='RS256').key
            claims=jwt.decode(encoded,key,algorithms=['RS256'],audience=self.settings.client_id,
                issuer=self.settings.issuer,leeway=15,
                options={'require':['exp','iat','sub','aud','iss','nonce']})
            if not isinstance(claims.get('nonce'),str) or not hmac.compare_digest(claims['nonce'].encode(),nonce.encode()):
                raise ValueError('OIDC nonce mismatch')
            if claims.get('azp',self.settings.client_id)!=self.settings.client_id:
                raise ValueError('OIDC authorized party mismatch')
            if isinstance(claims['aud'],list) and len(claims['aud'])>1 and claims.get('azp')!=self.settings.client_id:
                raise ValueError('OIDC multi-audience requires azp')
            subject=claims['sub']
            if not isinstance(subject,str) or not 1<=len(subject)<=255:
                raise ValueError('Invalid subject')
            return subject
