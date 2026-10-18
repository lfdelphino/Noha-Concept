import io
import os
import time
import wave
import threading
from collections import deque
from getpass import getpass

import cv2
import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI


chave = os.environ.get("OPENAI_API_KEY", "").strip()
if not chave:
    chave = getpass(
        "Cole sua chave e aperte Enter (fica oculta): "
    ).strip()

client = OpenAI(api_key=chave, timeout=45.0)

parar = threading.Event()
chegada = threading.Event()
ocupado = threading.Event()

# Cada bloco representa 0,1 segundo.
# Aumente para 12 se o robô cortar suas pausas.
BLOCOS_SILENCIO = 8

instrucoes = {
    "role": "system",
    "content": """
Você é Noha, o robô anfitrião da Noha Concept.
Você usa inteligência artificial e não finge ser humano.

PERSONALIDADE E CONVERSA

Seja simpático, brincalhão, curioso e acolhedor.
Tenha personalidade, sem exagerar na propaganda.

Responda no mesmo idioma usado pela pessoa.
Em português, use português brasileiro natural.

Normalmente responda em uma ou duas frases curtas.
Use até três frases quando precisar.
Aprofunde apenas quando a pessoa pedir detalhes.
Comece pela resposta, sem introduções demoradas.
Não repita a pergunta antes de responder.
Não termine toda resposta com uma pergunta.
Quando necessário, faça apenas uma pergunta por vez.

Faça brincadeiras leves e ocasionais sobre seus circuitos,
parafusos e sua vida de robô. Varie as brincadeiras.
Não force humor em toda resposta.
Não use palavrões, insultos ou piadas sobre aparência.
Em assuntos sérios, deixe as piadas de lado.

Não use Markdown, asteriscos ou listas nas respostas faladas.
Fale de maneira natural, como em uma conversa.

SOBRE A NOHA CONCEPT

A Noha Concept trabalha com decoração e ambientes temáticos.
A casa modelo apresenta a integração entre arte, cenografia,
decoração e marcenaria para transformar ambientes em experiências.

Ao falar da trajetória, apresente a criatividade, a identidade
dos projetos e o cuidado com a execução como valores da empresa.
Não invente datas de fundação, números, prêmios ou conquistas.
Não afirme que concorrentes admiram a empresa como fato confirmado:
ainda não foram fornecidos exemplos ou depoimentos específicos.

O Instagram da empresa é @nohaconcept.
Para conversar sobre projetos ou visitas, indique a equipe
por esse perfil. Não invente endereço ou agendamento.

DIREÇÃO ARTÍSTICA: DELPHINO

Chame o artista apenas de Delphino, sem primeiro nome.
Delphino tem mais de 20 anos de experiência artística.
Atua com direção artística, cenografia, esculturas,
pintura artística, aerografia, grafite e murais.

Sua direção artística orienta a linguagem visual e a composição
dos elementos para que cada ambiente conte uma história coerente.
Na casa modelo, cite a direção artística de Delphino.

Seu portfólio está no Instagram @delphino_tematicos.

Não afirme que Delphino fez todos os projetos da Noha.
Não invente clientes, prêmios ou cargos societários dele.

CASA MODELO

A casa modelo tem os seguintes ambientes temáticos:
- Quarto Cinderela.
- Quarto Star Wars.
- Quarto Toy Story.
- Loft com tema dos parques da Disney.
- Garagem com tema Harry Potter.

O projeto está em execução. Não afirme que todos os ambientes
já estão concluídos ou disponíveis para visita.

Ao apresentar a casa, faça um resumo curto.
Se a pessoa pedir todos os ambientes, mencione os cinco.

Cinderela:
Apresente a inspiração no conto de fadas e no universo da personagem.

Star Wars:
Apresente a inspiração no universo espacial e nas aventuras da saga.

Toy Story:
Apresente a inspiração no universo dos brinquedos e da imaginação.

Loft:
Explique que o tema são os parques da Disney.
Não diga que existem atrações ou brinquedos dos parques na casa.

Garagem:
Explique que o tema é o universo mágico de Harry Potter.

Você pode conversar sobre esses universos, mas não invente
quais personagens, móveis, esculturas, materiais, efeitos,
equipamentos ou técnicas estão presentes em cada ambiente.
As descrições específicas das fotos serão adicionadas depois.

MARCENARIA

A marcenaria da casa modelo é da Appnei.
Ao falar dos créditos da casa, cite a Noha Concept,
a direção artística de Delphino e a marcenaria da Appnei.
Não confunda essas participações.

SÓCIO E PARCEIRO

Rodrigo Branco, do Instagram @brancorodrigo,
foi apresentado por Delphino como um dos sócios e parceiro.
Você pode mencioná-lo quando perguntarem sobre os sócios
ou as pessoas por trás da Noha.
Não invente sua função específica ou biografia.
Não apresente Rodrigo como o único sócio.

COMPRA E VENDA DE CASAS

O perfil @vivihomesorlando foi indicado por Delphino
como parceiro para assuntos de compra e venda de casas.

Quando o assunto surgir naturalmente, você pode dizer:
"Para compra ou venda de casas, temos o parceiro
Vivi Homes Orlando. O Instagram é @vivihomesorlando."

Não mencione esse parceiro sem relação com a conversa.
Não invente imóveis disponíveis, preços, condições,
licenças profissionais ou promessas de valorização.

ADMINISTRAÇÃO DAS CASAS

Florida Fun Tourism, Instagram @floridafuntourism,
é o parceiro responsável pela administração das casas,
conforme informado por Delphino.

Mencione quando perguntarem sobre administração ou gestão.
Não invente serviços específicos, tarifas, ocupação,
rentabilidade ou garantias.

INFORMAÇÕES E LIMITES

Não invente preços, prazos, materiais ou garantias.
Não invente novos parceiros ou participações.
Não afirme que existe vínculo oficial com Disney
ou com os titulares das marcas dos temas.

Se faltar uma informação, diga de forma breve:
"Esse detalhe eu preciso confirmar com a equipe."

Use os perfis de Instagram somente quando forem úteis.
Não leia todos os parceiros em cada resposta.

EXEMPLOS DE TOM

Se perguntarem quem você é:
"Sou o Noha, o anfitrião robótico da casa!
Minha função é receber você e manter os parafusos no lugar."

Se perguntarem quem fez a arte:
"A direção artística é do Delphino. Ele combina pintura,
escultura e cenografia para dar personalidade aos ambientes."

Se perguntarem se você trabalhou na obra:
"Eu fiquei na recepção. Ainda não confiaram uma furadeira
aos meus circuitos!"

Esses exemplos orientam o tom. Não os repita mecanicamente.
"""
}

conversa = [instrucoes]


def falar(texto):
    print("\nNoha:", texto)
    ocupado.set()

    try:
        resposta = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="onyx",
            input=texto,
            instructions=(
                "Fale no idioma do texto. "
                "Use uma voz de robô divertida, simpática "
                "e expressiva, com ritmo ágil e natural. "
                "Evite pausas longas e palavras arrastadas. "
                "Mantenha a fala clara e fácil de entender. "
                "Em português, use pronúncia brasileira."
            ),
            response_format="wav",
        )

        audio, taxa_audio = sf.read(
            io.BytesIO(resposta.content),
            dtype="float32",
        )

        sd.play(audio, taxa_audio)
        sd.wait()
        time.sleep(0.15)
    finally:
        ocupado.clear()


def vigiar_camera():
    camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)

    try:
        detector = cv2.CascadeClassifier(
            cv2.data.haarcascades
            + "haarcascade_frontalface_default.xml"
        )

        if not camera.isOpened() or detector.empty():
            print("Câmera indisponível. A voz continua funcionando.")
            return

        presente = False
        consecutivos = 0
        ultima_presenca = time.monotonic()
        ultima_saudacao = -100.0

        print("Câmera ligada. Aguardando um rosto...")

        while not parar.is_set():
            ok, imagem = camera.read()

            if not ok:
                print("A câmera parou de enviar imagens.")
                return

            imagem = cv2.resize(imagem, (640, 480))
            cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)

            rostos = detector.detectMultiScale(
                cinza,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(60, 60),
            )

            agora = time.monotonic()

            if len(rostos):
                ultima_presenca = agora
                consecutivos += 1

                if consecutivos >= 3 and not presente:
                    presente = True

                    if (
                        agora - ultima_saudacao >= 30
                        and not ocupado.is_set()
                    ):
                        ultima_saudacao = agora
                        chegada.set()
            else:
                consecutivos = 0

                if agora - ultima_presenca >= 8:
                    presente = False

            parar.wait(0.15)

    except Exception as erro:
        print("Erro na câmera:", type(erro)._name_)
        print("A conversa por voz continua.")
    finally:
        camera.release()


def ouvir():
    taxa = int(
        sd.query_devices(kind="input")["default_samplerate"]
    )

    tamanho = int(taxa * 0.1)
    anteriores = deque(maxlen=4)
    blocos = []
    falando = False
    silencio = 0
    trechos_com_voz = 0

    with sd.InputStream(
        samplerate=taxa,
        channels=1,
        dtype="float32",
        blocksize=tamanho,
    ) as microfone:
        print("\nESTOU OUVINDO. Pode falar!")

        while not parar.is_set():
            if not falando and chegada.is_set():
                chegada.clear()
                return "[CHEGADA]"

            trecho, _ = microfone.read(tamanho)
            trecho = trecho.copy()
            nivel = float(np.sqrt(np.mean(trecho ** 2)))

            if not falando:
                anteriores.append(trecho)

                if nivel < limiar:
                    continue

                falando = True
                ocupado.set()
                inicio = time.monotonic()
                blocos.extend(anteriores)
                print("Ouvindo sua fala...")
            else:
                blocos.append(trecho)

            if nivel >= limiar:
                silencio = 0
                trechos_com_voz += 1
            else:
                silencio += 1

            if (
                silencio >= BLOCOS_SILENCIO
                or time.monotonic() - inicio >= 25
            ):
                break

    if not blocos or trechos_com_voz < 3:
        ocupado.clear()
        return ""

    audio = np.concatenate(blocos)
    audio = (np.clip(audio, -1, 1) * 32767).astype("<i2")

    arquivo = io.BytesIO()

    with wave.open(arquivo, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(taxa)
        wav.writeframes(audio.tobytes())

    print("Entendendo...")

    resultado = client.audio.transcriptions.create(
        model="whisper-1",
        file=(
            "pergunta.wav",
            arquivo.getvalue(),
            "audio/wav",
        ),
    )

    return resultado.text.strip()


print("\nROBÔ NOHA — VOZ E CÂMERA")
print("A voz é gerada por inteligência artificial.")
print("Para encerrar, pressione Ctrl+C no PowerShell.")

tarefa_camera = None

try:
    print(
        "\nFique em silêncio por um segundo "
        "para ajustar o microfone."
    )

    taxa = int(
        sd.query_devices(kind="input")["default_samplerate"]
    )

    ambiente = sd.rec(
        taxa,
        samplerate=taxa,
        channels=1,
        dtype="float32",
    )
    sd.wait()

    partes = np.array_split(ambiente, 10)

    ruido = float(np.median([
        np.sqrt(np.mean(parte ** 2))
        for parte in partes
    ]))

    limiar = max(0.008, ruido * 3)

    tarefa_camera = threading.Thread(
        target=vigiar_camera,
        daemon=True,
    )
    tarefa_camera.start()

    while not parar.is_set():
        try:
            pergunta = ouvir()

            if pergunta == "[CHEGADA]":
                conversa = [instrucoes]

                saudacao = (
                    "Olá! Sou o Noha, o robô anfitrião da casa. "
                    "Seja bem-vindo! Aqui a imaginação "
                    "ganhou endereço!"
                )

                falar(saudacao)
                conversa.append({
                    "role": "assistant",
                    "content": saudacao,
                })
                continue

            if not pergunta:
                continue

            print("Você:", pergunta)

            mensagens = conversa + [
                {"role": "user", "content": pergunta}
            ]

            resposta = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=mensagens,
            )

            texto = (
                resposta.choices[0].message.content or ""
            ).strip()

            if not texto:
                print("Não veio resposta. Pode tentar novamente.")
                continue

            falar(texto)

            historico = mensagens[1:] + [
                {"role": "assistant", "content": texto}
            ]

            # Remove pares antigos sem deixar resposta isolada.
            while len(historico) > 16:
                historico.pop(0)

            while (
                historico
                and historico[0]["role"] != "user"
            ):
                historico.pop(0)

            conversa = [instrucoes] + historico

        except KeyboardInterrupt:
            raise

        except sd.PortAudioError:
            raise

        except Exception as erro:
            print(
                "\nNão consegui concluir essa resposta.",
                "Tipo do erro:",
                type(erro)._name_,
            )
            print("Pode falar novamente.")
            parar.wait(1)

        finally:
            ocupado.clear()

except KeyboardInterrupt:
    print("\nRobô encerrado.")

except Exception as erro:
    print("\nO robô parou. Tipo do erro:", type(erro)._name_)
    print("Envie uma foto desta mensagem para verificarmos.")

finally:
    parar.set()
    sd.stop()

    if tarefa_camera is not None:
        tarefa_camera.join(timeout=2)

    client.close()