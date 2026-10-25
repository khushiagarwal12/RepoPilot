import requests, json, sys
url = 'http://127.0.0.1:8000/repositories/'
payload = {'github_url': 'https://github.com/psf/requests'}
resp = requests.post(url, json=payload)
print('Status', resp.status_code)
print(resp.text)
