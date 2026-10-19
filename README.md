# Noha — anfitrião de voz para Raspberry Pi 5

Aplicação sem câmera, com `gpt-realtime-1.5` por padrão, modelos de voz selecionáveis, microfone e alto-falante USB, sensor HC-SR501, painel de administração e configuração de Wi-Fi pelo celular. A personalidade e os fatos da Noha Concept foram extraídos de `robo.py`; o rascunho original permanece como referência.

O Pi captura/reproduz áudio e detecta movimento; a conversa depende da internet e da API OpenAI. Não há modelo local, ferramentas externas ou etapa separada de transcrição. GPT-Live utiliza sua própria conexão de áudio, sem um modelo de backend adicional.

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

## Alto-falante e cancelamento de eco

O microfone é o reSpeaker XVF3800; o alto-falante pode ser HDMI, a saída do Pi, um dispositivo USB ou a saída P2/JST do próprio XVF3800. Com `echo_cancel = "auto"` (padrão, seção `[audio]`), alto-falante ligado ao XVF3800 usa o cancelamento de eco do chip, que recebe o próprio áudio como referência, e o instalador remove o AEC do PipeWire para não processar o som duas vezes. Qualquer outra saída usa o AEC WebRTC do PipeWire (`noha_echo_source`/`noha_echo_sink`). Para trocar de alto-falante, reexecute o instalador com o novo nome de `noha audio-list` e reinicie:

```bash
sudo bash scripts/install.sh --user "$USER" --speaker 'alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_...'
```

`--echo-cancel pipewire` ou `--echo-cancel xvf3800` força um modo; `noha doctor` mostra qual está ativo.

## LEDs do reSpeaker XVF3800

Com o reSpeaker XVF3800 USB (firmware USB padrão), o anel de 12 LEDs mostra o estado da Noha: âmbar pulsando enquanto o sensor aquece, azul pulsando ao conectar, direção de quem fala (DoA) enquanto escuta, verde-água enquanto fala, arco-íris durante atualizações, vermelho em erros e apagado aguardando movimento. O instalador adiciona `python3-usb`, uma regra udev para o grupo `plugdev` e inclui o usuário nesse grupo; reinicie após instalar. `noha doctor` lê a versão do firmware para confirmar o acesso. Falhas de USB nos LEDs nunca interrompem a conversa. Ajuste em `/etc/noha/noha.toml`:

```toml
[leds]
enabled = true
brightness = 96 # 0-255
```

## Painel de administração

A versão **1.3.0 exige reexecutar o instalador uma vez** (LEDs, udev e grupo plugdev), assim como a 1.2.1 ao migrar da série 1.1, inclusive em Pis que já recebem atualizações automáticas. Copie o projeto ao Pi, execute `sudo bash scripts/install.sh --user "$USER"` e reinicie. O instalador adiciona Avahi/mDNS, ngrok e os serviços do painel. Não publica nem configura uma conta ngrok automaticamente.

Na mesma rede do Pi, abra **http://noha.local** no celular ou computador. Não precisa descobrir o IP atribuído pelo roteador nem conectar uma tela ao Pi. O nome funciona quando o Raspberry está conectado ao roteador como cliente Wi-Fi ou por Ethernet. Redes de convidados, isolamento de dispositivos ou bloqueio de multicast podem impedir a descoberta local. Para outro nome, use `--hostname outro-nome` no instalador; reinstalações preservam o nome registrado.

A senha inicial é **admin**. Altere em **Ajustes → Senha do painel**; a troca encerra as sessões existentes. Para recuperar pelo terminal, sem colocar a senha no histórico:

```bash
sudo noha dashboard-password
```

Digite a nova senha e confirme; Enter nas duas perguntas restaura `admin`. A senha fica armazenada como hash com salt, e reinstalações preservam a senha alterada. O acesso local usa HTTP, portanto senha e alterações trafegam sem criptografia na LAN. O endereço ngrok usa HTTPS.

O painel foi desenhado primeiro para celular, com quatro áreas:

- **Visão geral:** estado da voz, modelo ativo, conexão, prontidão de áudio/sensor, CPU, temperatura, memória, disco e os dois endereços do painel.
- **Atividade:** conversas, duração, tokens informados pelo modelo, erros e reconexões, hoje/7/30 dias e por modelo. O histórico numérico persiste por 90 dias em `/var/lib/noha-metrics`, sem áudio, transcrições ou estimativas de custo. Hoje segue o fuso do Pi. Contadores não fornecidos, como tokens de voz do GPT-Live, aparecem como indisponíveis.
- **Ajustes:** instruções do agente, modelo, voz compatível, GPIO BCM com pino físico, microfone, alto-falante e modo de cancelamento de eco, substituição da chave OpenAI, senha e configurações ngrok. Campos de chave vazios mantêm a credencial existente; valores salvos não são devolvidos ao navegador.
- **Sistema:** até 100 eventos recentes com filtros e acompanhamento ao vivo; versões instalada/disponível, última verificação, estado e botões para verificar/instalar atualizações.

Mudanças de voz, instruções, GPIO e chave aguardam a conversa atual terminar. O painel distingue configurações pendentes da configuração ativa. A gravação é atômica, com cópias em `/var/lib/noha/backups`; falha de inicialização restaura os arquivos anteriores. A mudança de GPIO verifica se a linha pode ser usada e não altera funções de hardware do sistema automaticamente. Desligue o Pi antes de mudar a fiação física.

O seletor inclui GPT Realtime 1.5, GPT Live 1, Realtime 2.1 Mini, 2.1, 2 e as famílias GPT-4o Realtime. Modelos legados recebem indicação; IDs retirados ficam desabilitados. O acesso depende da sua conta OpenAI e um erro não causa troca silenciosa de modelo. GPT-Live tem um adaptador separado; respostas faladas, interrupções e saudações precisam de validação com o hardware definitivo.

### Acesso ngrok sempre ativo

Crie sua conta ngrok e copie o **authtoken do agente** e o **hostname HTTPS atribuído à conta**. No painel local, abra **Ajustes → Acesso ngrok**, preencha os campos e salve. A integração usa authtoken, não a chave da API de gerenciamento ngrok. O plano gratuito atribui um hostname fixo; escolher um hostname próprio depende do plano da conta.

Depois de configurado, o serviço ngrok inicia em cada boot, mantém o endereço público em paralelo com `noha.local` e tenta reconectar após quedas. Se ainda não houver credenciais, fica aguardando configuração e o painel mostra esse estado. Não há encaminhamento de portas no roteador. Se trocar o hostname, abra o endereço novo: a resposta de gravação chega antes do reinício do túnel. Falhas do túnel não impedem o acesso local.

O ngrok recebe tráfego apenas para `http://127.0.0.1:80`. A configuração é gerada pelo sistema e validada antes de salvar; não aceita upstream arbitrário. Credenciais ficam fora dos releases, e a inspeção local de corpos de requisição está desativada. O plano gratuito tem limites de requisições/tráfego e pode apresentar um aviso inicial no navegador. O painel utiliza um fluxo de eventos e o suspende quando a página fica oculta, reduzindo requisições.

O mesmo processo web atende a porta 80 continuamente. Conexões destinadas ao endereço do hotspot recebem exclusivamente o portal de Wi-Fi. A escolha usa o endereço local da conexão, sem confiar no hostname enviado pelo navegador; o firewall do hotspot mantém a restrição. O processo web não é root. Um serviço separado, com socket Unix restrito, aplica apenas as operações administrativas permitidas.

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
| `/etc/noha/dashboard-password.json` | Hash da senha e revisão para invalidar sessões |
| `/etc/noha/ngrok.json`, `/etc/noha/ngrok.yml` | Authtoken e configuração gerada ngrok; acesso restrito |
| `/var/lib/noha-metrics/usage.sqlite3` | Métricas numéricas persistentes |
| `/opt/noha/current` | Link atômico para a versão ativa e seu ambiente Python |
| `/opt/noha/releases/` | Versões preparadas e anteriores, preservadas para rollback |
| `~/.config/pipewire/pipewire.conf.d/80-noha-echo.conf` | Rotas físicas do cancelamento de eco |
| `noha.service` (usuário) | Sensor, sessão de voz e áudio; inicia com lingering |
| `noha-network.service` (sistema) | NetworkManager, recuperação, DHCP/DNS e socket local |
| `noha-portal.service` (sistema) | Painel e portal Wi-Fi na porta 80, sempre ativo, sem root |
| `noha-admin.service` (sistema) | Operações administrativas por socket Unix restrito |
| `noha-ngrok.service` (sistema) | Acesso HTTPS ngrok, sempre ativo após configuração |
| `noha-update.timer` (sistema) | Verifica releases públicas a cada hora |
| `noha-update-check.timer` (sistema) | Verifica logo após o boot e repete se estiver offline |

Não altere somente `audio.microphone`/`audio.speaker` no TOML: reexecute o instalador com `--mic`/`--speaker` para atualizar também as rotas PipeWire. Dispositivos USB sem número de série podem mudar de nome ao trocar de porta; selecione-os novamente nesse caso.

Para cadastrar ou corrigir a chave sem expô-la no histórico:

```bash
sudoedit /etc/noha/secrets.env
systemctl --user restart noha
```

Alterações manuais de personalidade ou temporizadores exigem reiniciar o serviço. Alterações pelo painel são aplicadas quando a conversa termina. Para acompanhar estados, erros e contagem de tokens:

```bash
journalctl --user -u noha -f
sudo journalctl -u noha-network -u noha-portal -f
journalctl --user -u pipewire -u wireplumber -b
```

`noha doctor` verifica a configuração, presença da chave sem exibi-la, dispositivos e nós AEC, plugins GStreamer e função do GPIO. Ele não comprova qualidade acústica nem acesso à API. Use `pinctrl get 3` para inspecionar o pino. O portal também funciona se a chave estiver ausente; a voz informa o problema nos logs.

## Atualizações automáticas

O instalador habilita atualizações de [lfdelphino/Noha-Concept](https://github.com/lfdelphino/Noha-Concept), sem token GitHub. Uma consulta sem instalação é agendada cerca de 30 segundos após o boot; sem internet, tenta novamente a cada dois minutos. A instalação automática continua cerca de cinco a oito minutos após o boot e depois a cada hora, com pequeno atraso aleatório. O painel permite verificar ou solicitar instalação manual. Apenas releases estáveis com número maior que o instalado são aplicadas; commits e tags sem release não alteram o robô.

Para instalações anteriores a esta funcionalidade, copie a versão atual do projeto ao Pi e **reexecute o instalador uma vez**, depois reinicie. Isso migra os serviços para `/opt/noha/current`, habilita o timer e instala o atualizador independente do ambiente Python da aplicação.

O atualizador baixa `noha-pi.zip`, confere seu SHA-256 contra os metadados HTTPS do GitHub, rejeita caminhos/links inválidos no ZIP e prepara um ambiente Python separado. Durante o preparo, a versão atual continua funcionando. Não altera `/etc/noha`, a personalidade local, a chave, os perfis Wi-Fi nem as rotas físicas do áudio.

Antes da troca, bloqueia novas saudações e aguarda até cinco minutos pela conversa em andamento e operações de rede. Se o robô continuar ocupado ou sem hardware pronto, adia a atualização para a próxima verificação. Durante a troca, reinicia voz, rede, painel, administração e ngrok, verifica heartbeats, versão, dispositivos/GPIO e serviços durante pelo menos 15 segundos; não realiza chamadas pagas para testar a API. Configurações pendentes do painel são aplicadas antes da instalação de outra versão.

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
