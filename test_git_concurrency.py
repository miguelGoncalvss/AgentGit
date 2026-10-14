import os
import subprocess
import time
import shutil
from concurrent.futures import ProcessPoolExecutor

REPO_DIR = os.path.abspath("benchmark_git_concurrency")

def init_repo():
    if os.path.exists(REPO_DIR):
        shutil.rmtree(REPO_DIR)
    os.makedirs(REPO_DIR)
    subprocess.run(["git", "init"], cwd=REPO_DIR, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "AgentBench"], cwd=REPO_DIR, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agent@bench.local"], cwd=REPO_DIR, capture_output=True, check=True)
    
    # Initial commit
    init_file = os.path.join(REPO_DIR, "base.txt")
    with open(init_file, "w") as f:
        f.write("Initial repository state\n")
    subprocess.run(["git", "add", "."], cwd=REPO_DIR, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=REPO_DIR, capture_output=True, check=True)

def agent_worker(agent_id):
    file_path = os.path.join(REPO_DIR, f"agent_{agent_id}.txt")
    with open(file_path, "w") as f:
        f.write(f"Agent {agent_id} working on task {time.time()}\n")
    
    # Try git add
    t0 = time.time()
    add_res = subprocess.run(["git", "add", f"agent_{agent_id}.txt"], cwd=REPO_DIR, capture_output=True, text=True)
    if add_res.returncode != 0:
        return {"agent": agent_id, "stage": "add", "success": False, "error": add_res.stderr.strip(), "elapsed": time.time() - t0}
    
    commit_res = subprocess.run(["git", "commit", "-m", f"Agent {agent_id} commit"], cwd=REPO_DIR, capture_output=True, text=True)
    if commit_res.returncode != 0:
        return {"agent": agent_id, "stage": "commit", "success": False, "error": commit_res.stderr.strip(), "elapsed": time.time() - t0}
    
    return {"agent": agent_id, "stage": "done", "success": True, "error": "", "elapsed": time.time() - t0}

if __name__ == "__main__":
    init_repo()
    print("Initiating 10 concurrent agents committing to Git simultaneously...")
    with ProcessPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(agent_worker, range(10)))
    
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    print(f"\nResults: {len(successes)} succeeded, {len(failures)} failed due to concurrency locks:")
    for f in failures:
        print(f"  Agent {f['agent']} failed at [{f['stage']}]: {f['error']}")
    
    # Cleanup
    shutil.rmtree(REPO_DIR, ignore_errors=True)
