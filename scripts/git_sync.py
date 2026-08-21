import os
import sys
from dulwich import porcelain

repo_path = r"c:\Users\Yash H\Desktop\API-testing-AI-agent-main"
remote_url = "https://github.com/YashPrometteur/API-testing-AI-agent.git"

print("Checking repository...")
if not os.path.exists(os.path.join(repo_path, ".git")):
    print("Initializing git repository...")
    repo = porcelain.init(repo_path)
else:
    repo = porcelain.open_repo(repo_path)

print("Staging files...")
porcelain.add(repo_path, paths=["."])

status = porcelain.status(repo_path)
print("Staged files count:", len(status.staged["add"]) + len(status.staged["modify"]))

try:
    commit_id = porcelain.commit(
        repo_path,
        message="feat: Implement AI testing agent with HITL resume, severity normalization, reporting refresh, and actual_response support",
        author="YashPrometteur <yash@prometteursolutions.com>",
        committer="YashPrometteur <yash@prometteursolutions.com>"
    )
    print("Commit created:", commit_id.decode() if isinstance(commit_id, bytes) else commit_id)
except Exception as e:
    print("Commit info:", e)

# Configure remote
try:
    porcelain.remote_add(repo_path, "origin", remote_url)
    print(f"Added remote origin: {remote_url}")
except Exception as e:
    print(f"Remote origin status: {e}")

print("Repository ready for push.")
