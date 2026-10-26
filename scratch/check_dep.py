import os, json, urllib.request

token = ''
with open('.env.local', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if line.startswith('AI_WEBSITE_AGENT_VERCEL_TOKEN='):
            token = line.split('=', 1)[1].strip().strip('"').strip("'")
            break

team_id = 'team_QXGv3dhoM4GCLKdg15Sw3Lgs'
dep_id = 'dpl_3LwZ4trmU9torPG5Lu2NvCg4TsAW'

url = f'https://api.vercel.com/v13/deployments/{dep_id}?teamId={team_id}'
req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        print('State:', data.get('readyState'))
        print('Error:', data.get('error'))
        print('Inspector URL:', data.get('inspectorUrl'))
        print('Target:', data.get('target'))
except Exception as e:
    print('Error:', e)
