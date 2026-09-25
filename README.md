# mr: Merge Request pelo terminal

Cria um Merge Request no GitLab a partir da branch atual. O projeto é detectado pelo remoto `origin` do repositório em que você está, e a descrição pode vir de um template do projeto.

```powershell
mr develop                                   # branch atual -> develop
mr develop -dv1 "https://gitlab.sebrae.com.br/df/migrations-sgi/-/merge_requests/5215"
mr develop -dv2 --description "Corrige o filtro de atendimentos"
mr develop -dv2 --dry-run                    # mostra o MR sem criar
```

## Instalação

Requer **git** e o **uv**, o gerenciador de ferramentas Python. O uv baixa o Python sozinho, se precisar.

1. Instale o uv (uma vez só):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Instale o `mr`:

   ```powershell
   uv tool install git+https://gitlab.sebrae.com.br/df/mr-cli.git
   ```

   Se der erro de certificado (Zscaler), rode antes `$env:UV_NATIVE_TLS = "1"`.

3. Abra um terminal novo e configure o token:

   ```powershell
   mr --config
   ```

   Esse comando abre o arquivo `.env` pessoal (`%APPDATA%\mr\.env`). Preencha `GITLAB_TOKEN` com um Personal Access Token com escopo `api`, gerado em <https://gitlab.sebrae.com.br/-/user_settings/personal_access_tokens>.

Se preferir usar o pipx: `pipx install git+https://gitlab.sebrae.com.br/df/mr-cli.git`.

### Atualizar e desinstalar

```powershell
uv tool upgrade mr-cli
uv tool uninstall mr-cli
```

Se o `upgrade` não pegar a versão nova, reinstale: `uv tool install --force git+https://gitlab.sebrae.com.br/df/mr-cli.git`.

## Configuração pessoal (`%APPDATA%\mr\.env`)

| Variável | Para que serve |
|---|---|
| `GITLAB_TOKEN` | Token do GitLab (obrigatório) |
| `GITLAB_TARGET_BRANCH` | Branch de destino quando nenhuma for informada. Vazio = branch padrão do projeto |
| `GITLAB_ASSIGNEE` | Username(s) do assignee, separados por vírgula. Vazio = você |
| `GITLAB_REVIEWER` | Username(s) do reviewer, separados por vírgula |
| `GITLAB_CA_BUNDLE` | Caminho de um `.pem` (ex.: Zscaler), se houver erro de SSL |

## Opções

| Opção | Efeito |
|---|---|
| `-s BRANCH` | Branch de origem (padrão: a atual) |
| `-T "Título"` | Título (padrão: mensagem do último commit) |
| `--description "..."` | Texto da descrição. Nos templates, preenche `{{descricao}}` |
| `-a user` / `-r user` | Assignee / reviewer só para este MR |
| `--no-assign` / `--no-review` | Sem assignee / sem reviewer |
| `--draft` | Cria como Draft |
| `--push` | Faz `git push -u origin <branch>` antes |
| `--open` | Abre o MR no navegador |
| `--remove-source` | Marca para apagar a branch de origem após o merge |
| `--squash` | Marca squash dos commits |
| `--labels a,b` | Labels |
| `--dry-run` | Mostra o que seria criado, sem criar |

`mr --help` dentro de um projeto lista também os templates disponíveis para ele.

## Templates de descrição

Os templates do time ficam em [`src/mr_cli/mr_config.toml`](src/mr_cli/mr_config.toml), agrupados pelo caminho do projeto no GitLab. Cada template vira uma flag com o mesmo nome:

```toml
[projects."df/cerebro".templates.dv1]
help = "Com alteração de banco: informe o link do MR de banco"
args = ["link_banco"]          # valores da flag, na ordem: mr develop -dv1 "<link_banco>"
description = '''
### Possui Alteração de Banco de Dados?
- [x] Sim [{{link_banco}}]({{link_banco}})
'''
```

Estas tags são preenchidas automaticamente: `{{descricao}}`, `{{titulo}}`, `{{source}}`, `{{target}}` e `{{projeto}}`.

- **Template para outro projeto:** adicione uma seção `[projects."grupo/projeto".templates.<nome>]`, abra um MR neste repositório e aumente a `version` no `pyproject.toml`.
- **Templates só seus:** crie `%APPDATA%\mr\mr_config.toml` no mesmo formato. Um template com o mesmo nome substitui o do time.

## Desenvolvimento

```powershell
uv tool install --editable C:\caminho\para\mr-cli   # alterações no código valem na hora
```

Ao publicar uma mudança, aumente a `version` no `pyproject.toml` para que o `upgrade` a detecte.
