"""
Cria um Merge Request no GitLab a partir da branch atual do repositório.

O projeto é detectado pelo remoto "origin" do repositório em que o comando roda.

Uso:
    mr develop                          # branch atual -> develop
    mr develop -dv1 "https://..."       # usa o template "dv1" do projeto
    mr develop -dv2 --description "..." # template "dv2" preenchendo {{descricao}}
    mr develop -dv1 "https://..." --dry-run   # mostra o MR sem criar
    mr --config                         # mostra/abre os arquivos de configuração

Configuração (pasta do usuário, veja user_config_dir()):
    .env            -> token, assignee/reviewer padrão, SSL
    mr_config.toml  -> (opcional) templates próprios, somados aos templates do pacote
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from importlib import metadata, resources
from pathlib import Path

PACKAGE_DIR = resources.files("mr_cli")
BUNDLED_CONFIG = PACKAGE_DIR / "mr_config.toml"
ENV_EXAMPLE = PACKAGE_DIR / "env.example"
TAG_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def package_version() -> str:
    try:
        return metadata.version("mr-cli")
    except metadata.PackageNotFoundError:
        return "dev"


def user_config_dir() -> Path:
    """%APPDATA%\\mr no Windows, ~/.config/mr nos demais; MR_CONFIG_DIR sobrescreve."""
    if os.getenv("MR_CONFIG_DIR"):
        return Path(os.environ["MR_CONFIG_DIR"])
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "mr"
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "mr"


def user_env_file() -> Path:
    return user_config_dir() / ".env"


def user_templates_file() -> Path:
    return user_config_dir() / "mr_config.toml"


def ensure_user_env() -> bool:
    """Cria o .env do usuário a partir do exemplo. Retorna True se já existia."""
    env_file = user_env_file()
    if env_file.exists():
        return True
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return False


def open_config() -> int:
    existed = ensure_user_env()
    print(f"Configuração pessoal: {user_env_file()}" + ("" if existed else "  (criado agora)"))
    print(f"Templates pessoais:   {user_templates_file()}  (opcional)")
    print(f"Templates do pacote:  {BUNDLED_CONFIG}")
    if os.name == "nt":
        os.startfile(user_env_file())  # abre no editor padrão
    return 0


def load_env(path: Path) -> None:
    """Carrega KEY=VALUE do .env sem sobrescrever variáveis já definidas."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def split_list(value: str | None) -> list[str]:
    return [v.strip().lstrip("@") for v in (value or "").split(",") if v.strip()]


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} falhou")
    return result.stdout.strip()


def project_from_remote(repo: str, remote: str = "origin") -> tuple[str, str]:
    """Extrai (host, caminho do projeto) da URL do remoto.

    Aceita https://host/grupo/projeto.git, ssh://git@host:porta/grupo/projeto.git
    e git@host:grupo/projeto.git.
    """
    url = git(repo, "remote", "get-url", remote)
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        host, path = parsed.hostname, parsed.path
    else:
        host, _, path = url.partition("@")[2].partition(":")
    path = path.strip("/").removesuffix(".git")
    if not host or not path:
        raise RuntimeError(f"Não foi possível identificar o projeto a partir do remoto '{url}'")
    return host, path


def read_toml(path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"Erro de sintaxe em {path}: {e}") from None


def load_project_config(project_path: str) -> dict:
    """Seção [projects."<grupo/projeto>"] dos templates do pacote, com o
    mr_config.toml pessoal por cima (templates de mesmo nome são substituídos)."""
    merged: dict = {}
    for source in (BUNDLED_CONFIG, user_templates_file()):
        for key, cfg in read_toml(source).get("projects", {}).items():
            if key.lower() != project_path.lower():
                continue
            templates = {**merged.get("templates", {}), **cfg.get("templates", {})}
            merged = {**merged, **cfg, "templates": templates}
    return merged


def render_template(name: str, template: dict, values: list[str], builtins: dict) -> str:
    tags = template.get("args", [])
    if len(values) != len(tags):
        expected = ", ".join(tags) or "nenhum"
        raise RuntimeError(
            f"-{name} espera {len(tags)} valor(es) ({expected}), mas recebeu {len(values)}."
        )
    mapping = {**builtins, **dict(zip(tags, values))}
    unknown: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key in mapping:
            return mapping[key]
        unknown.append(key)
        return match.group(0)

    text = TAG_RE.sub(replace, template.get("description", "")).strip()
    if unknown:
        print(f"Aviso: tags sem valor no template -{name}: {', '.join(sorted(set(unknown)))}", file=sys.stderr)
    if builtins["descricao"] and "descricao" not in TAG_RE.findall(template.get("description", "")):
        text = f"{text}\n\n{builtins['descricao']}"
    return text


class GitLab:
    def __init__(self, base_url: str, token: str, project: str):
        self.api = base_url.rstrip("/") + "/api/v4"
        self.token = token
        self.project = urllib.parse.quote(project, safe="")
        self.ssl_context = self._build_ssl_context()

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        if os.getenv("GITLAB_SSL_VERIFY", "true").lower() in ("0", "false", "no"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ca_bundle = os.getenv("GITLAB_CA_BUNDLE")
        return ssl.create_default_context(cafile=ca_bundle or None)

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("PRIVATE-TOKEN", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitLab respondeu {e.code} em {method} {path}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Falha ao conectar em {self.api}: {e.reason}\n"
                "Se for erro de certificado, configure GITLAB_CA_BUNDLE no .env."
            ) from None

    def project_info(self) -> dict:
        return self.request("GET", f"/projects/{self.project}")

    def branch_exists(self, branch: str) -> bool:
        try:
            self.request("GET", f"/projects/{self.project}/repository/branches/{urllib.parse.quote(branch, safe='')}")
            return True
        except RuntimeError as e:
            if " 404 " in str(e):
                return False
            raise

    def current_user(self) -> dict:
        return self.request("GET", "/user")

    def user_by_username(self, username: str) -> dict:
        users = self.request("GET", "/users", params={"username": username})
        if not users:
            raise RuntimeError(f"Usuário '{username}' não encontrado no GitLab.")
        return users[0]

    def find_open_mr(self, source: str, target: str) -> dict | None:
        mrs = self.request(
            "GET",
            f"/projects/{self.project}/merge_requests",
            params={"state": "opened", "source_branch": source, "target_branch": target},
        )
        return mrs[0] if mrs else None

    def create_mr(self, payload: dict) -> dict:
        return self.request("POST", f"/projects/{self.project}/merge_requests", body=payload)


def build_parser(templates: dict, add_help: bool = True) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mr",
        add_help=add_help,
        description="Cria um Merge Request no GitLab a partir da branch atual.",
    )
    p.add_argument("target", nargs="?", help="Branch de destino (padrão: GITLAB_TARGET_BRANCH ou branch padrão do projeto)")
    p.add_argument("-s", "--source", help="Branch de origem (padrão: branch atual do git)")
    p.add_argument("-T", "--title", help="Título do MR (padrão: mensagem do último commit)")
    p.add_argument("--description", default="", help="Texto da descrição; em templates preenche a tag {{descricao}}")
    p.add_argument("-a", "--assignee", help="Username(s) do assignee, separados por vírgula (padrão: GITLAB_ASSIGNEE ou você)")
    p.add_argument("-r", "--reviewer", help="Username(s) do reviewer, separados por vírgula (padrão: GITLAB_REVIEWER)")
    p.add_argument("--no-assign", action="store_true", help="Não definir assignee")
    p.add_argument("--no-review", action="store_true", help="Não definir reviewer")
    p.add_argument("--repo", default=".", help="Caminho do repositório git local (padrão: pasta atual)")
    p.add_argument("--draft", action="store_true", help="Cria o MR como Draft")
    p.add_argument("--remove-source", action="store_true", help="Marcar para remover a branch de origem após o merge")
    p.add_argument("--squash", action="store_true", help="Marcar squash dos commits no merge")
    p.add_argument("--labels", default="", help="Labels separadas por vírgula")
    p.add_argument("--push", action="store_true", help="Faz 'git push -u origin <source>' antes de criar o MR")
    p.add_argument("--open", action="store_true", help="Abre o MR no navegador ao final")
    p.add_argument("--dry-run", action="store_true", help="Mostra o MR que seria criado, sem criá-lo")
    p.add_argument("--config", action="store_true", help="Mostra onde ficam as configurações e abre o .env pessoal")
    p.add_argument("--version", action="version", version=f"mr {package_version()}")

    if templates:
        group = p.add_argument_group("templates de descrição deste projeto (mr_config.toml)")
        exclusive = group.add_mutually_exclusive_group()
        for name, tpl in templates.items():
            tags = tpl.get("args", [])
            info = tpl.get("help", "")
            if tags:
                info = f"{info} [valores: {', '.join(tags)}]".strip()
            if tags:
                exclusive.add_argument(f"-{name}", dest=f"tpl_{name}", nargs=len(tags), metavar=tuple(t.upper() for t in tags), help=info or None)
            else:
                exclusive.add_argument(f"-{name}", dest=f"tpl_{name}", action="store_const", const=[], help=info or None)
    return p


def resolve_users(gl: GitLab, usernames: list[str]) -> list[dict]:
    return [gl.user_by_username(u) for u in usernames]


def main() -> int:
    load_env(user_env_file())

    # 1ª passada: só para descobrir --repo e, com ele, o projeto e seus templates.
    pre_args, _ = build_parser({}, add_help=False).parse_known_args()
    if pre_args.config:
        return open_config()
    repo = pre_args.repo

    try:
        git(repo, "rev-parse", "--is-inside-work-tree")
        in_repo = True
    except RuntimeError:
        in_repo = False

    project_path = ""
    base_url = os.getenv("GITLAB_URL", "https://gitlab.sebrae.com.br")
    if in_repo:
        try:
            host, project_path = project_from_remote(repo)
            base_url = f"https://{host}"
        except RuntimeError:
            project_path = os.getenv("GITLAB_PROJECT", "")

    project_cfg = load_project_config(project_path) if project_path else {}
    templates = project_cfg.get("templates", {})
    for name in templates:
        if not TEMPLATE_NAME_RE.match(name):
            raise RuntimeError(f"Nome de template inválido em mr_config.toml: '{name}' (use letras, números e _).")

    # 2ª passada: parser completo, com as flags -<template> do projeto.
    try:
        args = build_parser(templates).parse_args()
    except argparse.ArgumentError as e:
        raise RuntimeError(f"Conflito de nome de template em mr_config.toml: {e}") from None

    if not in_repo:
        print(f"Erro: '{Path(repo).resolve()}' não é um repositório git.", file=sys.stderr)
        return 1
    if not project_path:
        print("Erro: repositório sem remoto 'origin' e GITLAB_PROJECT não definido.", file=sys.stderr)
        return 1

    token = os.getenv("GITLAB_TOKEN", "").strip()
    if not token or token.startswith("seu_token"):
        created = not ensure_user_env()
        print(
            f"Erro: defina GITLAB_TOKEN em {user_env_file()}"
            + (" (arquivo criado agora)" if created else "")
            + "\nPara abrir o arquivo: mr --config",
            file=sys.stderr,
        )
        return 1

    gl = GitLab(base_url, token, project_path)

    source = args.source or git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if source == "HEAD":
        print("Erro: HEAD destacado; informe a branch com --source.", file=sys.stderr)
        return 1

    if args.push and not args.dry_run:
        print(f"Enviando '{source}' para o remoto...")
        git(repo, "push", "-u", "origin", source)

    project = gl.project_info()
    print(f"Projeto: {project['path_with_namespace']}")
    target = args.target or os.getenv("GITLAB_TARGET_BRANCH") or project["default_branch"]

    if source == target:
        print(f"Erro: origem e destino são a mesma branch ('{source}').", file=sys.stderr)
        return 1
    if not gl.branch_exists(source):
        msg = f"a branch '{source}' não existe no GitLab. Faça push ou use --push."
        if not args.dry_run:
            print(f"Erro: {msg}", file=sys.stderr)
            return 1
        print(f"Aviso: {msg}", file=sys.stderr)

    title = args.title
    if not title:
        try:
            title = git(repo, "log", "-1", "--pretty=%s", source)
        except RuntimeError:
            title = source
    if args.draft and not title.lower().startswith("draft:"):
        title = f"Draft: {title}"

    # Descrição: template escolhido (-dvX), senão o "default" do projeto, senão --description.
    chosen = [(n, getattr(args, f"tpl_{n}")) for n in templates if getattr(args, f"tpl_{n}") is not None]
    if not chosen and project_cfg.get("default"):
        default_name = project_cfg["default"]
        if default_name not in templates:
            raise RuntimeError(f"default = '{default_name}' não existe nos templates de {project_path}.")
        chosen = [(default_name, [])]
    builtins = {
        "descricao": args.description,
        "titulo": title,
        "source": source,
        "target": target,
        "projeto": project["path_with_namespace"],
    }
    if chosen:
        name, values = chosen[0]
        description = render_template(name, templates[name], values, builtins)
    else:
        description = args.description

    existing = gl.find_open_mr(source, target)
    if existing:
        print(f"Já existe um MR aberto de '{source}' para '{target}': !{existing['iid']}")
        print(existing["web_url"])
        if args.open:
            webbrowser.open(existing["web_url"])
        return 0

    payload = {
        "source_branch": source,
        "target_branch": target,
        "title": title,
        "description": description,
        "remove_source_branch": args.remove_source,
        "squash": args.squash,
    }
    if args.labels:
        payload["labels"] = args.labels

    assignees: list[dict] = []
    if not args.no_assign:
        names = split_list(args.assignee or os.getenv("GITLAB_ASSIGNEE"))
        assignees = resolve_users(gl, names) if names else [gl.current_user()]
        payload["assignee_ids"] = [u["id"] for u in assignees]

    reviewers: list[dict] = []
    if not args.no_review:
        names = split_list(args.reviewer or os.getenv("GITLAB_REVIEWER"))
        if names:
            reviewers = resolve_users(gl, names)
            payload["reviewer_ids"] = [u["id"] for u in reviewers]

    if args.dry_run:
        print(f"{source} -> {target}")
        print(f"Título:    {title}")
        print(f"Assignee:  {', '.join(u['username'] for u in assignees) or '-'}")
        print(f"Reviewer:  {', '.join(u['username'] for u in reviewers) or '-'}")
        if chosen:
            print(f"Template:  -{chosen[0][0]}")
        print("Descrição:")
        print("-" * 60)
        print(description or "(vazia)")
        print("-" * 60)
        print("(dry-run: nenhum MR foi criado)")
        return 0

    mr = gl.create_mr(payload)
    print(f"MR !{mr['iid']} criado: {source} -> {target}")
    print(mr["web_url"])
    if args.open:
        webbrowser.open(mr["web_url"])
    return 0


def cli() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    try:
        return main()
    except RuntimeError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
