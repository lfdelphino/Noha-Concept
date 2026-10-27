"""Plain-language health checks shared by the dashboard and the Pi's status board."""
ORDER = {"error": 0, "warn": 1, "info": 2, "ok": 3}
NETWORK = {"iniciando": "Verificando a conexão…", "buscando": "Buscando redes Wi-Fi…", "conectando": "Conectando ao Wi-Fi…",
           "reconectando": "Tentando as redes salvas…", "recuperando": "Recuperando a conexão…"}


STUCK_SECONDS = 600  # far beyond an HC-SR501 hold time: a pin held high this long is wiring, not visitors


def stuck_minutes(voice):
    """Minutes of uninterrupted motion when that is implausibly long, else None."""
    history = voice.get("motion_history") or []
    if voice.get("motion") and history and history[-1][1] is None:
        held = (voice.get("updated") or 0) - history[-1][0]
        if held >= STUCK_SECONDS:
            return int(held // 60)
    return None


def usd(value):
    return f"US$ {value:.2f}".replace(".", ",")


def checks(status):
    """[{id, label, level, detail, action}] worst first. Levels: error, warn, info, ok."""
    voice = status.get("voice") or {}
    network = status.get("network") or {}
    items = []

    def add(ident, label, level, detail, action=None):
        items.append({"id": ident, "label": label, "level": level, "detail": detail, "action": action})

    running = not voice.get("stale", True)
    if running:
        add("voz", "Serviço de voz", "ok", f"Em execução · versão {voice.get('version', '—')}")
    else:
        add("voz", "Serviço de voz", "error", "Parado ou reiniciando. Se continuar assim, veja os eventos.", "sistema/eventos")

    mode = network.get("mode")
    if mode == "online":
        add("internet", "Internet", "ok", f"Wi-Fi {network['wifi']}" if network.get("wifi") else "Conectada")
    elif mode == "configuracao":
        add("internet", "Internet", "error",
            f"Sem internet. Conecte um celular à rede {network.get('ssid') or 'Noha-Setup'} para escolher o Wi-Fi.")
    elif mode in NETWORK:
        add("internet", "Internet", "warn", NETWORK[mode])
    else:
        add("internet", "Internet", "error", "Controlador de rede indisponível.", "sistema/eventos")

    if status.get("key_configured"):
        add("chave", "Chave OpenAI", "ok", "Configurada")
    else:
        add("chave", "Chave OpenAI", "error", "Sem chave, Noha não conversa. Informe a chave nos ajustes.", "ajustes/chave")

    if running:
        missing = voice.get("audio_missing") or []
        if missing:
            add("audio", "Áudio", "error", f"Não encontrado: {', '.join(missing)}. Confira cabos e a seleção.", "ajustes/audio")
        elif voice.get("audio_ready"):
            add("audio", "Áudio", "ok", "Microfone e alto-falante prontos")
        else:
            add("audio", "Áudio", "warn", "Verificando os dispositivos…")

        if voice.get("gpio_ready") is False:
            add("sensor", "Sensor de presença", "error",
                f"Sem leitura no GPIO {voice.get('gpio', '—')}. Confira a ligação do sensor.", "ajustes/sensor")
        elif (stuck := stuck_minutes(voice)) is not None:
            gpio = voice.get("gpio")
            cause = (f" No GPIO {gpio} a placa do Pi tem um resistor que mantém o pino em nível alto: ligue o OUT "
                     "do sensor no GPIO 4 (pino 7) e escolha GPIO 4 nos ajustes." if gpio in (2, 3) else
                     " Confira a ligação do OUT e, no sensor, diminua o tempo e a sensibilidade.")
            add("sensor", "Sensor de presença", "warn",
                f"Indica movimento sem parar há {stuck} min. Se não há ninguém, o sinal está preso.{cause}", "ajustes/sensor")
        elif status.get("presence") is False:
            add("sensor", "Sensor de presença", "warn", "Saudações por movimento desligadas.", "inicio")
        else:
            add("sensor", "Sensor de presença", "ok", f"Ligado · GPIO {voice.get('gpio', '—')}")

        count = voice.get("local_greetings") or 0
        if not voice.get("greeting_local", True):
            add("saudacoes", "Saudações", "info", "O modelo gera cada saudação, com custo.", "ajustes/saudacoes")
        elif count:
            add("saudacoes", "Saudações", "ok", f"{count} gravadas no robô, sem custo")
        else:
            add("saudacoes", "Saudações", "warn",
                "Ainda não gravadas: por enquanto o modelo cumprimenta, com custo. Precisa de internet e chave.")

        if voice.get("setup_ready"):
            add("offline", "Ajuda sem internet", "ok", "Instruções de Wi-Fi gravadas na voz da Noha")
        elif voice.get("offline_voice"):
            add("offline", "Ajuda sem internet", "info", "Instruções de Wi-Fi pela voz offline, até a voz da Noha ser gravada")
        else:
            add("offline", "Ajuda sem internet", "warn",
                "Sem instruções faladas de Wi-Fi até a primeira gravação. Instale espeak-ng no Pi.")

    budget = status.get("budget") or {}
    limit, spent = budget.get("limit") or 0, budget.get("spent") or 0
    if limit <= 0:
        add("custo", "Custo de hoje", "info", f"{usd(spent)} · sem limite diário", "ajustes/custos")
    elif spent >= limit:
        add("custo", "Custo de hoje", "error", f"Limite de {usd(limit)} atingido. Noha volta à meia-noite.", "ajustes/custos")
    elif spent >= 0.8 * limit:
        add("custo", "Custo de hoje", "warn", f"{usd(spent)} de {usd(limit)}", "ajustes/custos")
    else:
        add("custo", "Custo de hoje", "ok", f"{usd(spent)} de {usd(limit)}")

    if (status.get("security") or {}).get("default_password"):
        add("senha", "Senha do painel", "error", "A senha padrão ainda está em uso. Troque-a agora.", "sistema/seguranca")

    updates = status.get("updates") or {}
    if updates.get("state") == "available":
        add("versao", "Atualização", "info", f"Versão {updates.get('latest', '').lstrip('v')} disponível", "sistema/atualizacoes")
    elif updates.get("state") in {"error", "rollback", "installer_required", "offline"}:
        add("versao", "Atualização", "warn", updates.get("message") or "Verifique o atualizador.", "sistema/atualizacoes")

    tunnel = (status.get("ngrok") or {}).get("state")
    if tunnel == "online":
        add("remoto", "Acesso pela internet", "ok", "Ativo")
    elif tunnel == "reconectando":
        add("remoto", "Acesso pela internet", "warn", "Reconectando…", "sistema/acesso")
    else:
        add("remoto", "Acesso pela internet", "info", "Não configurado. Configure para abrir o painel fora do local.",
            "sistema/acesso")
    return sorted(items, key=lambda item: ORDER[item["level"]])
