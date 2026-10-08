#!/usr/bin/env python3
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


def main():
    if os.geteuid() != 0:
        sys.exit('Run on the isolated staging Linux host as root.')
    directory = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('guard', directory / 'staging-guard.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    state = Path(os.environ.get('AVITY_CRM_STAGING_ROOT', '/var/lib/avity-crm-staging')).resolve()
    values = guard.validate_environment(state / 'avity-crm.env')
    origin = values['SERVER_URL']
    credentials = json.loads((state / 'admin.json').read_text())
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    token = None

    def request(query, variables=None, endpoint='metadata', authenticated=True, requested_origin=origin):
        headers = {'Content-Type':'application/json'}
        if requested_origin is not None:
            headers['Origin'] = requested_origin
        if authenticated and token:
            headers['Authorization'] = 'Bearer ' + token
        payload = json.dumps({'query':query,'variables':variables or {}}).encode()
        client = opener if authenticated else urllib.request.build_opener()
        try:
            response = client.open(urllib.request.Request(origin+'/'+endpoint, data=payload, headers=headers), timeout=90)
            return {**json.load(response), '_http_status': response.status}
        except urllib.error.HTTPError as error:
            return {**json.load(error), '_http_status': error.code}

    def data(query, variables=None, endpoint='metadata'):
        response = request(query, variables, endpoint)
        if response.get('errors'):
            codes = [e.get('extensions',{}).get('code','UNKNOWN') for e in response['errors']]
            raise RuntimeError('GraphQL rejected operation: ' + ','.join(codes))
        return response['data']

    phase = sys.argv[1]
    if phase == 'bootstrap':
        if (state/'qualification.json').exists():
            sys.exit('Bootstrap already recorded; use check instead.')
        signup = data('mutation($email:String!,$password:String!){signUp(email:$email,password:$password){tokens{accessOrWorkspaceAgnosticToken{token}}}}',
                      {k:credentials[k] for k in ('email','password')})
        token = signup['signUp']['tokens']['accessOrWorkspaceAgnosticToken']['token']
        created = data('mutation($input:SignUpInNewWorkspaceInput){signUpInNewWorkspace(input:$input){loginToken{token}workspace{id workspaceUrls{subdomainUrl customUrl}}}}',
                       {'input':{'displayName':'Avity-CRM staging synthétique'}})['signUpInNewWorkspace']
        if created['workspace']['workspaceUrls']['subdomainUrl'].rstrip('/') != origin:
            raise RuntimeError('Unexpected workspace URL.')
        exchanged = data('mutation($loginToken:String!,$origin:String!){getAuthTokensFromLoginToken(loginToken:$loginToken,origin:$origin){tokens{accessOrWorkspaceAgnosticToken{token}}}}',
                         {'loginToken':created['loginToken']['token'],'origin':origin})
        token = exchanged['getAuthTokensFromLoginToken']['tokens']['accessOrWorkspaceAgnosticToken']['token']
        activation = data('mutation{activateWorkspace(data:{}){id activationStatus}}')['activateWorkspace']
        if activation['activationStatus'] != 'ACTIVE':
            raise RuntimeError('Workspace did not activate.')
        (state/'qualification.json').write_text(json.dumps({'workspace_id':activation['id']},indent=2)+'\n')
        print('PASS synthetic workspace activation')
        return

    login = data('mutation($email:String!,$password:String!,$origin:String!){getLoginTokenFromCredentials(email:$email,password:$password,origin:$origin){loginToken{token}}}',
                 {**{k:credentials[k] for k in ('email','password')},'origin':origin})
    exchanged = data('mutation($loginToken:String!,$origin:String!){getAuthTokensFromLoginToken(loginToken:$loginToken,origin:$origin){tokens{accessOrWorkspaceAgnosticToken{token}}}}',
                     {'loginToken':login['getLoginTokenFromCredentials']['loginToken']['token'],'origin':origin})
    token = exchanged['getAuthTokensFromLoginToken']['tokens']['accessOrWorkspaceAgnosticToken']['token']
    oracle = json.loads((state/'qualification.json').read_text())
    if phase == 'create':
        name = 'AVITY-STAGING-SYNTHETIC-BACKUP-A'
        company = data('mutation($input:CompanyCreateInput!){createCompany(data:$input){id name}}',{'input':{'name':name}},'graphql')['createCompany']
        company = data('mutation($id:UUID!,$data:CompanyUpdateInput!){updateCompany(id:$id,data:$data){id name}}',
                       {'id':company['id'],'data':{'name':name+'-EDITED'}},'graphql')['updateCompany']
        oracle.update(company_id=company['id'], company_name=company['name'])
        (state/'qualification.json').write_text(json.dumps(oracle,indent=2)+'\n')
        print('PASS synthetic company create and update')
    elif phase in ('check','restored'):
        company = data('query($filter:CompanyFilterInput!){company(filter:$filter){id name}}',{'filter':{'id':{'eq':oracle['company_id']}}},'graphql')['company']
        if company != {'id':oracle['company_id'],'name':oracle['company_name']}:
            raise RuntimeError('Synthetic record persistence mismatch.')
        anonymous = request('query{companies{edges{node{id}}}}',endpoint='graphql',authenticated=False)
        if not anonymous.get('errors'):
            raise RuntimeError('Anonymous business access accepted.')
        unknown = request('mutation($email:String!,$password:String!){signUp(email:$email,password:$password){tokens{accessOrWorkspaceAgnosticToken{token}}}}',
                          {'email':'uninvited@staging.avity.invalid','password':'Synthetic-rejected-password-8961!'},authenticated=False)
        if not any(e.get('extensions',{}).get('code') == 'FORBIDDEN' for e in unknown.get('errors',[])):
            raise RuntimeError('Uninvited signup was not refused.')
        health = data('query{getSystemHealthStatus{services{id status}}getQueueMetrics(queueName:"workspace-queue"){workers details{failed waiting active}}getInstanceAndAllWorkspacesUpgradeStatus{instanceUpgradeStatus{inferredVersion health}upToDateWorkspaceCount workspacesBehind{id}workspacesFailed{id}}}',endpoint='admin-panel')
        if any(s['status'] != 'OPERATIONAL' for s in health['getSystemHealthStatus']['services']):
            raise RuntimeError('Dependency health failure.')
        if health['getQueueMetrics']['workers'] < 1 or health['getQueueMetrics']['details']['failed']:
            raise RuntimeError('Worker not healthy.')
        upgrade = health['getInstanceAndAllWorkspacesUpgradeStatus']
        if upgrade['workspacesBehind'] or upgrade['workspacesFailed'] or not upgrade['upToDateWorkspaceCount']:
            raise RuntimeError('Workspace upgrade incomplete.')
        # Token authentication bypasses cookie-CSRF; force the cookie path for these probes.
        token = None
        for forbidden_origin in (None,'http://127.0.0.1:3021'):
            response = request('query{currentUser{id}}',requested_origin=forbidden_origin)
            if response.get('_http_status') != 403 or response.get('error') != 'CSRF_ORIGIN_MISMATCH':
                raise RuntimeError('Cookie CSRF origin check failed.')
        print('PASS record, login, anonymous/signup refusals, cookie CSRF, dependency/worker/upgrade health')
    elif phase == 'mutate':
        data('mutation($id:UUID!,$data:CompanyUpdateInput!){updateCompany(id:$id,data:$data){id name}}',
             {'id':oracle['company_id'],'data':{'name':'AVITY-STAGING-SYNTHETIC-AFTER-SNAPSHOT-B'}},'graphql')
        print('Synthetic record changed after snapshot')
    elif phase == 'reset-record':
        data('mutation($id:UUID!,$data:CompanyUpdateInput!){updateCompany(id:$id,data:$data){id name}}',
             {'id':oracle['company_id'],'data':{'name':oracle['company_name']}},'graphql')
        print('Synthetic record reset to the qualification oracle')
    else:
        raise RuntimeError('Unknown qualification phase.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exceptions may contain sensitive HTTP request information; emit only their class.
        sys.exit('Qualification failed: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__))
