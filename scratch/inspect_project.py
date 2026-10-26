import os, json, urllib.request

token = ''
with open('.env.local', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if line.startswith('AI_WEBSITE_AGENT_VERCEL_TOKEN='):
            token = line.split('=', 1)[1].strip().strip('"').strip("'")
            break

team_id = 'team_QXGv3dhoM4GCLKdg15Sw3Lgs'
project_id = 'prj_KXSX2XqjBsbsjSXKKAXXbDrnYJRd'

url = f'https://api.vercel.com/v9/projects/{project_id}?teamId={team_id}'
req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode('utf-8'))
    print('Project Name:', data.get('name'))
    print('Framework:', data.get('framework'))
    print('Root Directory:', data.get('rootDirectory'))
    print('Output Directory:', data.get('outputDirectory'))
    print('Build Command:', data.get('buildCommand'))
    print('Dev Command:', data.get('devCommand'))
    print('Install Command:', data.get('installCommand'))
    print('Link:', data.get('link'))
    print('Latest Deployments:', data.get('latestDeployments'))
