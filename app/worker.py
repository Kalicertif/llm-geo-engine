import os
import time

def main():
    # Worker minimal (placeholder) : on branchera ensuite l’automatisation (analyse, drafts, social)
    env = os.getenv("ENVIRONMENT", "unknown")
    print(f"[worker] started (ENVIRONMENT={env})", flush=True)
    while True:
        print("[worker] heartbeat", flush=True)
        time.sleep(60)

if __name__ == "__main__":
    main()
