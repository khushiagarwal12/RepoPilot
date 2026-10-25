import requests, time, json

BASE_URL = "http://127.0.0.1:8000"

def ingest():
    resp = requests.post(f"{BASE_URL}/repositories/", json={"github_url": "https://github.com/psf/requests"})
    print("Ingest response:", resp.status_code, resp.text)
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    return data.get("repository_id"), data.get("job_id")

def wait_job(repo_id):
    for _ in range(30):
        time.sleep(2)
        try:
            jobs = requests.get(f"{BASE_URL}/repositories/{repo_id}/jobs").json()
            for job in jobs:
                if job.get("status") == "succeeded":
                    return True
        except Exception as e:
            print("Error checking jobs:", e)
    return False

def search(repo_id):
    resp = requests.post(f"{BASE_URL}/repositories/{repo_id}/search", json={"query": "session", "top_k": 5})
    print("Search response:", resp.status_code, resp.text)

if __name__ == "__main__":
    repo_id, job_id = ingest()
    if not repo_id:
        print("Ingestion failed")
    else:
        print(f"Repo ID: {repo_id}, Job ID: {job_id}")
        if wait_job(repo_id):
            print("Ingestion job succeeded, performing search...")
            search(repo_id)
        else:
            print("Ingestion job did not finish in time")
