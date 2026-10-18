# Noha — anfitrião de voz para Raspberry Pi 5

Aplicação nova, sem câmera, com `gpt-realtime-1.5`, microfone e alto-falante USB, sensor HC-SR501 e configuração de Wi-Fi pelo celular. A personalidade e os fatos da Noha Concept foram extraídos de `robo.py`; o rascunho original permanece como referência.

O Pi captura/reproduz áudio e detecta movimento; a conversa depende da internet e da API OpenAI. Não há modelo local, ferramentas externas ou etapa separada de transcrição.

## Instalação

Requer Raspberry Pi 5, Raspberry Pi OS **Desktop ARM64 Bookworm ou Trixie**, NetworkManager, PipeWire, acesso inicial à internet para instalar pacotes e uma chave de API com acesso ao modelo. A instalação não precisa do login automático do desktop. Use a mesma conta do desktop para evitar disputa pelo dispositivo de áudio.

Copie esta pasta para o Pi, conecte os dispositivos USB e execute no terminal:

```bash
sudo bash scripts/install.sh --user "$USER"
sudo reboot
```

O instalador pede o país do Wi-Fi quando necessário, mostra os nomes estáveis dos dispositivos de áudio e solicita a chave de API com entrada oculta. Não precisa colar a chave no código. A instalação também cria a fonte/saída de cancelamento de eco e verifica se o PipeWire as publicou.

Para informar os dispositivos explicitamente, use os valores `name` de `noha audio-list`:

```bash
sudo bash scripts/install.sh --user "$USER" --mic 'alsa_input.usb-...' --speaker 'alsa_output.usb-...'
```

Reexecutar o instalador preserva personalidade, chave, redes salvas e demais opções existentes. Arquivos Noha que precisarem ser substituídos têm cópias em `/var/lib/noha/backups`. Não atualiza automaticamente o sistema operacional. Instale primeiro com acesso local ao Pi: caso fique sem internet, o serviço de recuperação trocará o Wi-Fi para modo de configuração.

Depois de reiniciar:

```bash
noha doctor
noha audio-list
noha trigger
noha status
systemctl --user status noha
sudo systemctl status noha-network
```

`noha trigger` simula uma entrada na sala e aciona o serviço da conta atual. Execute comandos de áudio e voz como o usuário do desktop, **sem sudo**. Para rodar em primeiro plano, pare a instância do serviço:

```bash
systemctl --user stop noha
set -a
. /etc/noha/secrets.env
set +a
noha run --simulate-motion
# Ao terminar com Ctrl+C:
systemctl --user start noha
```

## Ligações do HC-SR501

Desligue a alimentação para conectar os fios. Identifique os pinos pelas inscrições no módulo, pois a ordem física varia entre placas.

| HC-SR501 | Raspberry Pi 5 |
|---|---|
| VCC | 5 V, pino físico 2 ou 4 |
| GND | GND, por exemplo pino físico 6 |
| OUT (saída lógica de 3,3 V) | **BCM GPIO3, pino físico 5** |

Não conecte 5 V ao GPIO. O pino físico 3 é GPIO2 e não é o pino escolhido neste projeto. O GPIO3 possui pull-up físico; o programa usa a saída ativa do PIR sem adicionar pull-down por software.

GPIO3 também é SCL do I²C. O instalador verifica conflito antes de prosseguir. Se não há nenhum dispositivo I²C necessário:

```bash
sudo bash scripts/install.sh --user "$USER" --disable-i2c
sudo reboot
sudo bash scripts/install.sh --user "$USER"
```

Comece com o temporizador do HC-SR501 baixo e sensibilidade moderada. Há 60 segundos de estabilização antes do primeiro disparo. O PIR detecta mudança de movimento/calor, não identifica pessoas nem confirma que alguém saiu.

## Conversa e áudio

- Movimento inicia uma sessão e uma saudação curta em português brasileiro.
- Noha segue o idioma do visitante, responde em poucas frases e mantém os fatos da personalidade.
- Pode ouvir durante a fala. Quando o visitante interrompe, esvazia os buffers de reprodução e informa à API quanto do áudio foi realmente reproduzido, usando uma estimativa conservadora do relógio de áudio.
- Fecha a sessão após 60 segundos sem fala do visitante, depois de concluir a resposta em andamento. A câmera nunca é utilizada.
- Para outra saudação, exige 30 segundos contínuos sem movimento e 120 segundos desde a saudação anterior. Movimento durante a conversa não inicia outra sessão.
- Áudio não é enviado enquanto o robô está ocioso. Gravações e transcrições não são persistidas; o histórico da sessão fica na API durante a conexão e não é reconstruído após uma queda.
- Falhas temporárias têm até cinco tentativas com espera crescente. Chave inválida, falta de saldo e falta de permissão são tratadas como erros da API, sem ativar o hotspot.

O caminho de áudio é:

```text
Microfone USB → PipeWire AEC → GStreamer PCM 24 kHz → OpenAI Realtime
Alto-falante USB ← PipeWire AEC (referência) ← GStreamer ← áudio da API
```

O cancelamento de eco depende do ambiente, volume e posicionamento. Mantenha microfone afastado do alto-falante, comece com volume moderado e confirme que Noha não responde à própria voz. Dispositivos são selecionados pelo `node.name`, não pelo número temporário da placa. A desconexão USB interrompe a sessão e provoca novas tentativas; não troca automaticamente para outro microfone.

## Configuração Wi-Fi pelo celular

Após 90 segundos sem internet, conecte o celular à rede **aberta** `Noha-Setup-XXXX`. O sufixo é específico do Pi. O sistema pode abrir a página automaticamente; se não abrir, use **http://192.168.50.1**. Confirme no celular que deseja permanecer conectado a uma rede sem internet.

Escolha uma rede da lista ou digite o SSID, marque se ela é oculta, selecione a segurança e informe a senha. Suporta redes abertas, WPA/WPA2 Personal e WPA3 Personal. Redes corporativas 802.1X, WEP e login em portais de hotéis não fazem parte desta versão.

Ao pressionar Conectar, a página explica o próximo passo e aguarda três segundos antes de desligar o hotspot. O Pi tenta a conexão por até 45 segundos, depois verifica a internet. Se falhar, a rede Noha-Setup volta; reconecte-se e reabra a página para ler o resultado. Uma associação válida sem internet é salva para novas tentativas. Uma tentativa com credenciais inválidas não apaga redes anteriores.

A busca de redes também pode interromper a conexão com o celular. O Pi usa um único rádio: não mantém o hotspot enquanto tenta entrar no Wi-Fi. A cada cinco minutos, se a página estiver sem interação há dois minutos, tenta até três perfis salvos, alternando os perfis entre tentativas. Essa tentativa pode deixar o hotspot indisponível por cerca de dois minutos e meio. Ethernet com internet também permite encerrar a configuração.

A rede aberta permite configuração por quem estiver por perto, conforme a opção escolhida para o projeto. O portal não recebe nem mostra a chave OpenAI. Durante o hotspot, regras temporárias `nftables` limitam a interface a DHCP, DNS e HTTP do portal; não há compartilhamento de internet nem acesso SSH por essa interface. Regras de firewall externas que bloqueiem essas portas precisam permitir o portal.

As verificações de internet exigem HTTP 204 via HTTPS nos endereços em `network.connectivity_urls`; bloqueio desses endereços pode ativar o portal mesmo que outros sites funcionem. Use endpoints de conectividade acessíveis na rede local. Erros da API OpenAI não são usados como teste de internet.

## Configurações e serviços

| Local | Finalidade |
|---|---|
| `/etc/noha/noha.toml` | Modelo, voz, dispositivos, GPIO, temporizadores e rede |
| `/etc/noha/personality.txt` | Personalidade e informações da Noha Concept |
| `/etc/noha/secrets.env` | `OPENAI_API_KEY=...`, somente root e grupo `noha` |
| `/opt/noha/current` | Link atômico para a versão ativa e seu ambiente Python |
| `/opt/noha/releases/` | Versões preparadas e anteriores, preservadas para rollback |
| `~/.config/pipewire/pipewire.conf.d/80-noha-echo.conf` | Rotas físicas do cancelamento de eco |
| `noha.service` (usuário) | Sensor, sessão de voz e áudio; inicia com lingering |
| `noha-network.service` (sistema) | NetworkManager, recuperação, DHCP/DNS e socket local |
| `noha-portal.service` (sistema) | Portal sem privilégios de root, ativo só no hotspot |
| `noha-update.timer` (sistema) | Verifica releases públicas a cada hora |

Não altere somente `audio.microphone`/`audio.speaker` no TOML: reexecute o instalador com `--mic`/`--speaker` para atualizar também as rotas PipeWire. Dispositivos USB sem número de série podem mudar de nome ao trocar de porta; selecione-os novamente nesse caso.

Para cadastrar ou corrigir a chave sem expô-la no histórico:

```bash
sudoedit /etc/noha/secrets.env
systemctl --user restart noha
```

Após modificar personalidade ou temporizadores, reinicie o serviço. Para acompanhar estados, erros e contagem de tokens:

```bash
journalctl --user -u noha -f
sudo journalctl -u noha-network -u noha-portal -f
journalctl --user -u pipewire -u wireplumber -b
```

`noha doctor` verifica a configuração, presença da chave sem exibi-la, dispositivos e nós AEC, plugins GStreamer e função do GPIO. Ele não comprova qualidade acústica nem acesso à API. Use `pinctrl get 3` para inspecionar o pino. O portal também funciona se a chave estiver ausente; a voz informa o problema nos logs.

## Atualizações automáticas

O instalador habilita atualizações de [lfdelphino/Noha-Concept](https://github.com/lfdelphino/Noha-Concept), sem token GitHub. Verifica a primeira vez cerca de cinco a oito minutos após o boot e depois a cada hora, com pequeno atraso aleatório. Apenas releases estáveis com número maior que o instalado são aplicadas; commits e tags sem release não alteram o robô.

Para instalações anteriores a esta funcionalidade, copie a versão atual do projeto ao Pi e **reexecute o instalador uma vez**, depois reinicie. Isso migra os serviços para `/opt/noha/current`, habilita o timer e instala o atualizador independente do ambiente Python da aplicação.

O atualizador baixa `noha-pi.zip`, confere seu SHA-256 contra os metadados HTTPS do GitHub, rejeita caminhos/links inválidos no ZIP e prepara um ambiente Python separado. Durante o preparo, a versão atual continua funcionando. Não altera `/etc/noha`, a personalidade local, a chave, os perfis Wi-Fi nem as rotas físicas do áudio.

Antes da troca, bloqueia novas saudações e aguarda até cinco minutos pela conversa em andamento e operações de rede. Se o robô continuar ocupado ou sem hardware pronto, adia a atualização para a próxima verificação. Durante a troca, reinicia voz e rede, verifica heartbeats, versão, dispositivos/ GPIO e serviços durante pelo menos 15 segundos; não realiza chamadas pagas para testar a API.

Uma falha de inicialização restaura o link e os arquivos de serviço anteriores. Uma interrupção de energia durante a troca é detectada por `noha-update-recover.service` no próximo boot, antes dos serviços do robô. Uma release rejeitada fica bloqueada até uma nova publicação ou tentativa manual. Os diretórios anteriores ficam preservados; acompanhe o espaço livre ao longo do tempo.

```bash
sudo noha-update --check       # consulta sem baixar/instalar uma nova versão
sudo noha-update               # tenta atualizar agora, respeitando conversa em andamento
sudo noha-update --retry       # tenta novamente a release que falhou
sudo noha-update --rollback    # volta à versão anterior registrada, também aguardando ociosidade
systemctl list-timers noha-update.timer
sudo journalctl -u noha-update -u noha-update-recover -b
```

Sem internet ou com GitHub indisponível, a aplicação instalada permanece em uso. Para pausar novas verificações, use `sudo systemctl disable --now noha-update.timer`, ou altere `enabled = false` em `/etc/noha/updates.toml`. Se já houver uma atualização em andamento, deixe-a concluir; a próxima tentativa será desativada.

O bootstrap `/opt/noha/updater.py` e a infraestrutura de boot são atualizados pelo instalador, não pela aplicação. Mudanças em pacotes nativos, formato do atualizador, GPIO físico ou rotas PipeWire exigem reexecutar o instalador. Para sinalizar uma release que exige isso, gere o pacote com `--requires-installer`; o atualizador automático a recusará antes de trocar serviços.

### Publicar uma nova versão

O repositório contém `.github/workflows/release.yml`. Depois de enviar o projeto ao GitHub, o fluxo é:

1. Faça as alterações e aumente `version` em `pyproject.toml`, por exemplo de `1.1.0` para `1.1.1`.
2. Faça commit e envie a branch.
3. Crie e envie o tag correspondente:

```bash
git tag v1.1.1
git push origin v1.1.1
```

O GitHub Actions executa os testes, verifica se o tag coincide com o projeto, gera o ZIP e publica uma release com o pacote e checksum. Os Pis recebem essa versão na próxima verificação, quando estiverem ociosos. Se GitHub Actions estiver desabilitado no repositório, habilite-o antes de enviar o tag.

Para gerar o mesmo pacote manualmente, use `python scripts/build_release.py --tag v1.1.0` (ajuste o tag à versão atual), e anexe `dist/noha-pi.zip` e `dist/noha-pi.zip.sha256` à release correspondente. O SHA-256 para instalação automática é lido dos metadados do asset publicados pelo GitHub; se ainda não estiver disponível, o Pi aguarda uma nova verificação.

## Verificação rápida

No computador de desenvolvimento, testes de lógica sem hardware ou chamadas pagas:

```bash
python -m venv .venv
# Linux: .venv/bin/python; Windows: .venv\Scripts\python
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

No Pi, valide uma vez com o hardware definitivo:

1. Reinicie sem entrar no desktop. Aguarde o aquecimento do PIR e confirme os serviços ativos.
2. Entre na sala: deve haver uma saudação, sem repetições enquanto conversa.
3. Pergunte em português e interrompa uma resposta. A fala deve parar, sem Noha confundir sua própria voz com o visitante.
4. Fique em silêncio por 60 segundos; confirme o encerramento nos logs. Aguarde o intervalo sem movimento e teste outra chegada.
5. Desconecte/reconecte USB; confira a recuperação usando os mesmos dispositivos.
6. Remova a internet e aguarde 90 segundos; configure pelo celular. Teste senha errada, depois correta, e confirme que a rede anterior permanece salva.
7. Reinicie novamente e confirme conexão e voz sem intervenção.

## Remoção e restauração

```bash
sudo python3 scripts/uninstall.py
```

Remove somente os serviços, launcher, configuração AEC e perfil de hotspot pertencentes ao Noha. Preserva os perfis Wi-Fi de clientes, pacotes compartilhados, código, chave e backups. Desativa lingering somente se ele não estava ativo antes da instalação. Não reativa I²C automaticamente.

Para voltar a uma configuração anterior, pare o serviço, copie o arquivo correspondente de `/var/lib/noha/backups/<data>/` ao local original e reinicie o serviço afetado. Os backups são acessíveis somente por root. Para reinstalar, execute novamente o instalador.

Referências: [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations), [PipeWire echo cancellation](https://pipewire.pages.freedesktop.org/pipewire/page_module_echo_cancel.html), [Raspberry Pi networking](https://www.raspberrypi.com/documentation/computers/configuration.html).
