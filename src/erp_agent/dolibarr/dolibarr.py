import shutil
import subprocess
import tarfile
from pathlib import Path


class DolibarrError(Exception):
    pass


class Dolibarr:
    def __init__(
        self,
        baseline_dir: str | Path,
        docs_dir: str | Path,
        db_root_user: str,
        db_root_password: str,
        db_container_name: str = "dolibarr-mariadb-1",
        db_name: str = "dolidb",
        admin_login: str = "admin",
        admin_password: str = "admin",
        timeout: float = 120.0,
    ):
        self.admin_login = admin_login
        self.admin_password = admin_password
        self.baseline_sql = Path(baseline_dir) / "db.sql"
        self.baseline_docs = Path(baseline_dir) / "documents.tar.gz"
        self.docs_dir = Path(docs_dir)
        self.db_root_user = db_root_user
        self.db_root_password = db_root_password
        self.db_container_name = db_container_name
        self.db_name = db_name
        self.timeout = timeout

    def reset(self) -> None:
        for path in (self.baseline_sql, self.baseline_docs):
            if not path.is_file():
                raise DolibarrError(f"Baseline file not found: {path}. Run checkpoint.sh first.")

        self._mariadb("-e", f"DROP DATABASE IF EXISTS `{self.db_name}`; CREATE DATABASE `{self.db_name}`;")
        with self.baseline_sql.open("rb") as sql:
            self._mariadb(self.db_name, stdin=sql)

        for child in self.docs_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        with tarfile.open(self.baseline_docs) as tar:
            tar.extractall(self.docs_dir, filter="tar")

    def query(self, sql: str) -> list[list[str]]:
        out = self._mariadb("-N", "-B", self.db_name, "-e", sql)
        return [line.split("\t") for line in out.splitlines()]

    def _mariadb(self, *args: str, stdin=subprocess.DEVNULL) -> str:
        cmd = [
            "docker", "exec", "-i", self.db_container_name,
            "mariadb", f"-u{self.db_root_user}", f"-p{self.db_root_password}", *args,
        ]
        try:
            result = subprocess.run(
                cmd, stdin=stdin, capture_output=True, encoding="utf-8", check=True, timeout=self.timeout
            )
        except subprocess.CalledProcessError as e:
            raise DolibarrError(f"mariadb exited with {e.returncode}: {e.stderr.strip()}") from e
        except subprocess.TimeoutExpired as e:
            raise DolibarrError(f"mariadb timed out after {self.timeout} s") from e
        return result.stdout
