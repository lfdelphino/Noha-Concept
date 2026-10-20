# Noha — anfitrião de voz para Raspberry Pi 5

Noha recebe os visitantes da casa modelo da Noha Concept por voz: percebe quem chega com um sensor de presença, cumprimenta, responde dúvidas sobre a casa em português (ou no idioma do visitante) e se despede. Não usa câmera nem tela; tudo que o visitante precisa ouvir, inclusive como configurar o Wi-Fi, é dito pelo alto-falante.

A equipe acompanha e ajusta o robô pelo **painel**, no celular ou no computador: estado ao vivo, visitas, custos, voz, personalidade, saudações, sensor e sistema. A conversa usa a API Realtime da OpenAI (`gpt-realtime-2.1-mini` por padrão, o modelo mais barato); o Pi cuida do áudio, do sensor, da rede e do painel. A personalidade e os fatos da Noha Concept vieram do rascunho original, `robo.py`, mantido como referência.

## Como uma visita acontece

1. **Presença confirmada.** Quem só passa gera uma detecção curta e é ignorado. Noha cumprimenta depois de 6 s de movimento contínuo ou de 2 detecções em 20 s.
2. **Saudação gravada, sem custo.** Uma das frases de saudação toca do próprio robô. Elas são sintetizadas uma vez, na voz escolhida, e ficam salvas no Pi.
3. **Escuta local.** Por até 10 s Noha ouve, ainda sem chamar a API. Se ninguém responde, a visita termina sem custo.
4. **Conversa.** Quando a pessoa fala, a conversa Realtime começa com o início da fala preservado e sem repetir a saudação. Termina após 30 s de silêncio, por despedida ou pelo limite da visita.
5. **Sem internet.** Se a pessoa fala e o robô está offline, Noha explica em voz alta como conectá-lo pelo celular: a rede `Noha-Setup-XXXX` deste Pi, soletrada, e o endereço do portal. Sem nenhuma gravação ainda (primeiro boot offline), cumprimenta e explica com a voz offline do `espeak-ng`.

Nada de áudio ou texto é guardado: o painel mostra só números (visitas, duração, respostas, tokens e custo estimado). Um limite diário de custo (US$ 2 por padrão) impede novas conversas quando atingido.

## O painel

Na mesma rede do Pi, abra **http://noha.local**. Pela internet, use o endereço ngrok configurado em **Sistema → Acesso pela internet**.

**Primeiro acesso.** A senha inicial é `admin` e só funciona na rede do local: pela internet ela é recusada. Depois de entrar, o painel pede uma senha nova antes de continuar. Para redefinir no próprio Pi, sem a senha atual: `sudo noha dashboard-password`.

| Página | Para quê |
|---|---|
| **Início** | O que Noha está fazendo agora; saudações por movimento ligadas/desligadas; **Testar saudação**; volume do alto-falante; visitas, conversas, custo e tempo de hoje; sensor ao vivo com as detecções dos últimos 10 minutos; diagnóstico com o caminho para resolver cada problema. |
| **Atividade** | Hoje, 7 ou 30 dias: visitas, quantas conversaram, custo e tempo; gráfico por hora ou dia; como as visitas terminaram; lista das visitas recentes; uso por modelo. |
| **Ajustes** | Voz e personalidade, frases de saudação, conversa, sensor de presença, áudio e LEDs, custos e chave OpenAI. Cada grupo salva separadamente; Noha aplica quando estiver livre e reinicia a voz em alguns segundos. Alterações pendentes podem ser descartadas. |
| **Sistema** | Diagnóstico completo, atualizações, acesso pela internet (ngrok), rede, saúde do Raspberry Pi, senha do painel e eventos ao vivo com filtros. |

Os valores salvos ficam em `/etc/noha`, com cópia em `/var/lib/noha/backups`. Se a voz não reiniciar com a configuração nova, a anterior volta sozinha e o painel avisa. Chaves salvas nunca voltam ao navegador.

O acesso local usa HTTP; a senha trafega sem criptografia na rede do local. O endereço ngrok usa HTTPS.

### Tela do próprio Pi

O robô não precisa de tela. Se houver uma ligada ao Pi, o instalador abre nela o **painel do robô**: um quadro só de leitura, em letras grandes, com o estado atual, visitas e custo de hoje, o sensor e os itens do diagnóstico. Ajustes nunca são feitos por ela. O quadro entra com um token próprio (`/etc/noha/kiosk-token`) que só funciona em um navegador no próprio Pi.

```bash
sudo python3 scripts/kiosk.py --user "$USER"           # ativa
sudo python3 scripts/kiosk.py --user "$USER" --remove  # desativa e revoga o token
```

O instalador configura o login automático do desktop e a tela sem descanso, o que também mantém disponível a saída de áudio HDMI. Use `--no-kiosk` na instalação para não configurar.

## Instalação

Requer Raspberry Pi 5, Raspberry Pi OS **Desktop ARM64 Bookworm ou Trixie**, NetworkManager, PipeWire, internet durante a instalação e uma chave de API OpenAI com acesso ao modelo. Use a mesma conta do desktop para evitar disputa pelo dispositivo de áudio.

Copie esta pasta para o Pi, conecte os dispositivos USB e execute:

```bash
sudo bash scripts/install.sh --user "$USER"
sudo reboot
```

O instalador pede o país do Wi-Fi quando necessário, mostra os dispositivos de áudio e a chave de API com entrada oculta, cria o cancelamento de eco e instala o painel, o acesso ngrok, o mDNS (`noha.local`), a voz offline `espeak-ng` e as atualizações automáticas. Para informar os dispositivos sem perguntas, use os nomes de `noha audio-list`:

```bash
sudo bash scripts/install.sh --user "$USER" --mic 'alsa_input.usb-...' --speaker 'alsa_output.usb-...'
```

Opções: `--hostname outro-nome` (padrão `noha`), `--echo-cancel auto|pipewire|xvf3800`, `--country BR`, `--no-kiosk`, `--disable-i2c`. Reexecutar o instalador preserva personalidade, chave, senha, redes salvas e ajustes.

Depois de reiniciar, sem sudo:

```bash
noha doctor          # configuração, chave, áudio, GPIO e LEDs
noha trigger         # simula a chegada de um visitante
noha status          # estado publicado pelo serviço de voz
sudo noha sensor off # desliga as saudações por movimento (on para religar)
```

Para rodar a voz em primeiro plano:

```bash
systemctl --user stop noha
set -a; . /etc/noha/secrets.env; set +a
noha run --simulate-motion
systemctl --user start noha   # depois de Ctrl+C
```

## Hardware

### Sensor de presença (HC-SR501)

Desligue a alimentação para conectar. Confira os pinos pelas inscrições do módulo.

| HC-SR501 | Raspberry Pi 5 |
|---|---|
| VCC | 5 V, pino físico 2 ou 4 |
| GND | GND, por exemplo pino físico 6 |
| OUT (3,3 V) | **GPIO 3, pino físico 5** |

Não ligue 5 V ao GPIO. O GPIO 3 também é o SCL do I²C; o instalador detecta o conflito e, sem dispositivos I²C, `--disable-i2c` libera o pino (reinicie e rode o instalador de novo).

Para ignorar quem só passa:

- Jumper em **H** (redisparo): a saída fica alta enquanto houver movimento.
- Temporizador (Tx) no mínimo, cerca de 3 s, e **Movimento contínuo para confirmar** acima disso (Ajustes → Sensor de presença).
- Sensibilidade (Sx) reduzida e o sensor apontado para onde o visitante para, não para o corredor. Fita sobre parte da lente estreita o cone.
- Acompanhe na página Início: as detecções aparecem mesmo com as saudações desligadas.

Um radar mmWave **LD2410** funciona no mesmo GPIO (saída OUT de 3,3 V), detecta pessoas paradas e tem alcance configurável: é a melhor troca para ignorar quem passa longe. O HC-SR04 mede distância (TRIG/ECHO) e não é compatível sem outro código.

### Alto-falante e cancelamento de eco

O microfone é o reSpeaker XVF3800. O alto-falante pode ser HDMI, a saída do Pi, USB ou a saída P2/JST do próprio XVF3800. No modo automático, um alto-falante ligado ao XVF3800 usa o cancelamento de eco do chip; qualquer outro usa o AEC WebRTC do PipeWire. Troque microfone, alto-falante ou modo em **Ajustes → Áudio e LEDs**: o painel reescreve as rotas do PipeWire e reinicia o áudio. Mantenha o microfone afastado do alto-falante e comece com volume moderado.

```text
Microfone → AEC → GStreamer PCM 24 kHz → OpenAI Realtime
Alto-falante ← AEC (referência) ← GStreamer ← áudio da API
```

### LEDs do reSpeaker XVF3800

O anel mostra o estado: âmbar pulsando ao aquecer, azul ao conectar, a direção de quem fala ao escutar, verde-água ao falar, arco-íris em atualizações, vermelho em erros e apagado aguardando. Brilho e liga/desliga ficam em Ajustes. Falhas de USB nos LEDs nunca interrompem a conversa.

## Wi-Fi pelo celular

Após 90 segundos sem internet, o Pi abre a rede **aberta** `Noha-Setup-XXXX` (sufixo próprio deste Pi). Basta falar com a Noha para ouvir os passos. Conecte o celular a essa rede; a página de configuração costuma abrir sozinha, senão use **http://192.168.50.1**, e confirme que quer continuar numa rede sem internet.

Escolha a rede ou digite o SSID, a segurança e a senha. Suporta redes abertas, WPA/WPA2 e WPA3 Personal; redes corporativas 802.1X, WEP e portais de hotel não. Ao conectar, o hotspot desliga, o Pi tenta por até 45 s e verifica a internet; se falhar, a rede de configuração volta para você ver o resultado. Credenciais erradas não apagam redes salvas. A cada cinco minutos sem uso da página, o Pi tenta de novo até três redes salvas.

Durante o hotspot, regras `nftables` limitam a interface a DHCP, DNS e à página de configuração, sem acesso à internet nem SSH por ela. A internet é verificada por respostas HTTP 204 em `network.connectivity_urls`.

## Arquivos e serviços

| Local | Conteúdo |
|---|---|
| `/etc/noha/noha.toml` | Todos os ajustes (o painel edita este arquivo, preservando comentários) |
| `/etc/noha/personality.txt` | Personalidade e fatos da Noha Concept |
| `/etc/noha/secrets.env` | `OPENAI_API_KEY`, somente root e grupo `noha` |
| `/etc/noha/presence.json` | Saudações por movimento ligadas ou desligadas |
| `/etc/noha/dashboard-password.json` | Hash da senha do painel |
| `/etc/noha/ngrok.json`, `ngrok.yml` | Acesso pela internet |
| `/var/lib/noha-metrics/usage.sqlite3` | Métricas e visitas, 90 dias, só números |
| `~/.cache/noha/greetings` | Saudações e instruções de Wi-Fi gravadas |
| `/opt/noha/current` | Versão ativa; versões anteriores em `/opt/noha/releases` |
| `noha.service` (usuário) | Voz, sensor, áudio e LEDs |
| `noha-network.service` | Wi-Fi, recuperação e hotspot |
| `noha-portal.service` | Painel, quadro do robô e portal Wi-Fi na porta 80, sem root |
| `noha-admin.service` | Operações administrativas por socket Unix restrito |
| `noha-ngrok.service` | Túnel HTTPS do acesso pela internet |
| `noha-update.timer`, `noha-update-check.timer` | Atualizações automáticas |

O processo web não roda como root: as operações privilegiadas passam por um serviço separado, que aceita apenas as operações do painel. Para editar à mão, use `sudoedit` e reinicie a voz (`systemctl --user restart noha`).

Para acompanhar:

```bash
journalctl --user -u noha -f
sudo journalctl -u noha-network -u noha-portal -u noha-admin -f
```

Cada saudação registra o piso e o pico de ruído (`piso_dbfs`, `pico_dbfs`): use-os para ajustar **Sensibilidade à voz** se Noha perder respostas ou conectar com ruído.

## Atualizações automáticas

O Pi instala releases estáveis de [lfdelphino/Noha-Concept](https://github.com/lfdelphino/Noha-Concept): verifica logo após o boot e depois a cada hora. A troca é imediata, inclusive durante uma conversa. O atualizador confere o SHA-256 do pacote, prepara a versão nova em paralelo, reinicia os serviços e verifica por pelo menos 15 s; se a versão nova não subir, volta à anterior sozinho. Não altera `/etc/noha`, a chave nem as redes.

```bash
sudo noha-update --check     # só consulta
sudo noha-update             # atualiza agora
sudo noha-update --retry     # tenta de novo uma versão rejeitada
sudo noha-update --rollback  # volta à versão anterior
```

Mudanças em pacotes do sistema, no atualizador de boot ou na fiação exigem reexecutar o instalador; gere essas releases com `--requires-installer` para o Pi recusá-las automaticamente.

### Publicar uma versão

1. Aumente `version` em `pyproject.toml`.
2. Faça commit e envie a branch; o GitHub Actions roda os testes em Python 3.11 e 3.13.
3. Crie e envie o tag: `git tag v2.0.0 && git push origin v2.0.0`.

O fluxo de release testa, confere o tag, gera `noha-pi.zip` e publica a release. Testes travados falham em minutos, sem segurar a release.

## Desenvolvimento

Testes de lógica, sem hardware nem chamadas pagas:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .          # Windows: .venv\Scripts\python
.venv/bin/python -m unittest discover -s tests -v
```

Para ver o painel e o quadro com dados de exemplo, sem Pi:

```bash
python scripts/dev_dashboard.py --scenario ok   # conversando | offline | problemas | primeiro-acesso
```

Abra http://localhost:8080 (senha `noha`; no cenário `primeiro-acesso`, `admin`) e http://localhost:8080/kiosk?token=dev para o quadro. O painel é HTML, CSS e módulos JavaScript em `noha/web`, sem etapa de build. Os ajustes vêm do esquema em `noha/settings.py`: um campo novo ali aparece no painel, é validado e gravado no TOML.

### Validação no Pi

1. Reinicie sem entrar no desktop e confira o diagnóstico na página Início.
2. Passe pela frente do sensor sem parar: não deve haver saudação. Pare na frente: a saudação toca.
3. Não responda: a visita aparece como "Só passou", sem custo. Responda: a conversa começa sem repetir a saudação.
4. Interrompa uma resposta: a fala para sem Noha confundir a própria voz com o visitante.
5. Desconecte e reconecte o USB: o diagnóstico mostra e depois limpa o problema.
6. Tire a internet, fale com a Noha: ela explica a configuração; siga os passos pelo celular.
7. Reinicie e confirme que tudo volta sem intervenção.

## Remoção

```bash
sudo python3 scripts/uninstall.py
```

Remove serviços, launcher, AEC, quadro e hotspot do Noha. Preserva redes Wi-Fi, pacotes, código, chave, métricas e backups (`/var/lib/noha/backups`, só root).

Referências: [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations), [OpenAI text-to-speech](https://developers.openai.com/api/docs/guides/text-to-speech), [PipeWire echo cancellation](https://pipewire.pages.freedesktop.org/pipewire/page_module_echo_cancel.html), [Raspberry Pi networking](https://www.raspberrypi.com/documentation/computers/configuration.html).
