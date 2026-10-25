import requests, sys, time
BASE_URL = "http://127.0.0.1:8000"
repo_id = 1
job_id = 7
url = f"{BASE_URL}/repositories/{repo_id}/jobs/{job_id}"
try:
    resp = requests.get(url, timeout=10)
    print(f"GET {url} -> {resp.status_code}")
    print(resp.text)
except Exception as e:
    print(f"Error: {e}")
