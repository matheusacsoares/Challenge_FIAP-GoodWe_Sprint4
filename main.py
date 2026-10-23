# Sprint 4 (COA + SERS) - GoodWe Smart Charging Station
# Estacao com 3 veiculos eletricos disputando a energia solar disponivel.
# O Pico le a geracao (LDR), distribui a potencia entre os EVs e mostra
# o resultado em LEDs e rele (por veiculo), LCD I2C e monitor serial.

import time
from machine import Pin, ADC, I2C

# ---------------- hardware ----------------
# LEDs de cada EV: (verde, amarelo, vermelho)
LEDS_EV = [
    (Pin(0, Pin.OUT), Pin(1, Pin.OUT), Pin(2, Pin.OUT)),
    (Pin(3, Pin.OUT), Pin(4, Pin.OUT), Pin(5, Pin.OUT)),
    (Pin(6, Pin.OUT), Pin(7, Pin.OUT), Pin(10, Pin.OUT)),
]

RELES_EV = [Pin(16, Pin.OUT), Pin(17, Pin.OUT), Pin(18, Pin.OUT)]  # contator de cada EV
botaoCenario = Pin(14, Pin.IN, Pin.PULL_UP)     # troca o cenario de energia
botaoReset = Pin(13, Pin.IN, Pin.PULL_UP)       # reinicia a bateria dos EVs
adcLdr = ADC(Pin(27))                           # LDR = geracao solar

i2c = I2C(0, sda=Pin(8), scl=Pin(9), freq=100000)
LCD_ADDR = 0x27

# ---------------- parametros ----------------
GERACAO_MAX_W = 8000        # geracao solar que o LDR pode representar
POTENCIA_MIN_W = 1500       # potencia minima para um EV carregar
CAPACIDADE_BAT_WH = 10000   # capacidade (simplificada) da bateria de cada EV
DT_SIM_S = 300              # cada ciclo representa 5 min de carga simulada
INTERVALO_MS = 2000         # tempo real entre ciclos
FATOR_CO2_KG_KWH = 0.0385   # fator medio da rede brasileira (conferir ano/fonte no README)

# cenarios: (nome, energia fixa em W; None = le do LDR)
CENARIOS = [
    ("AUTO", None),
    ("ALTA", 8000),
    ("LIMITADA", 5000),
    ("BAIXA", 2000),
]

# dados dos veiculos ficam na RAM, nesta lista de dicionarios
BAT_INICIAL = [20.0, 45.0, 70.0]
PEDIDOS_W = [2000, 2500, 3000]


def criarVeiculos():
    lista = []
    for i in range(3):
        lista.append({
            "id": "EV" + str(i + 1),
            "bateria": BAT_INICIAL[i],   # %
            "pedidoNominal": PEDIDOS_W[i],
            "pedido": PEDIDOS_W[i],      # W solicitados agora
            "concedido": 0,              # W entregues agora
            "estado": "ESPERA",
            "energiaWh": 0.0,
            "leds": LEDS_EV[i],
            "rele": RELES_EV[i],
        })
    return lista


# ---------------- display LCD I2C (PCF8574, 4 bits) ----------------
class LcdI2C:
    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr
        time.sleep_ms(50)
        for n in (0x03, 0x03, 0x03, 0x02):
            self._nibble(n, 0)
            time.sleep_ms(5)
        for c in (0x28, 0x0C, 0x06, 0x01):
            self._byte(c, 0)
            time.sleep_ms(5)

    def _write(self, b):
        self.i2c.writeto(self.addr, bytes([b | 0x08]))   # 0x08 = backlight

    def _nibble(self, n, rs):
        b = ((n & 0x0F) << 4) | rs
        self._write(b | 0x04)    # E = 1
        time.sleep_us(50)
        self._write(b & ~0x04)   # E = 0
        time.sleep_us(50)

    def _byte(self, v, rs):
        self._nibble(v >> 4, rs)
        self._nibble(v & 0x0F, rs)

    def linha(self, num, texto):
        self._byte(0x80 | (0x40 if num else 0x00), 0)
        texto = (texto + " " * 16)[:16]
        for ch in texto:
            self._byte(ord(ch), 1)


try:
    lcd = LcdI2C(i2c, LCD_ADDR)
except OSError:
    lcd = None
    print("[AVISO] LCD nao encontrado no I2C.")


# ---------------- entrada ----------------
# LDR -> Watts (mais luz = mais geracao)
def lerGeracaoSolar():
    return round((adcLdr.read_u16() / 65535) * GERACAO_MAX_W)


# detecta clique (quando o botao e solto)
def lerClique(pino, estavaPressionado):
    agora = (pino.value() == 0)
    return (estavaPressionado and not agora), agora


# ---------------- algoritmo de gerenciamento ----------------
# 1) prioridade: bateria mais baixa primeiro
# 2) cada EV, em ordem de prioridade, recebe a potencia minima se ainda houver energia
# 3) a energia que sobra e dividida proporcionalmente ao que ainda falta pedir
def distribuirEnergia(disponivel, veiculos):
    for v in veiculos:
        v["concedido"] = 0
        v["pedido"] = 0 if v["bateria"] >= 100 else v["pedidoNominal"]

    fila = [v for v in veiculos if v["pedido"] > 0]
    fila.sort(key=lambda v: v["bateria"])

    restante = disponivel
    for v in fila:
        base = min(POTENCIA_MIN_W, v["pedido"])
        if restante < base:
            break
        v["concedido"] = base
        restante -= base

    atendidos = [v for v in fila if v["concedido"] > 0]
    falta = sum(v["pedido"] - v["concedido"] for v in atendidos)
    if falta > 0 and restante > 0:
        fator = min(1.0, restante / falta)
        for v in atendidos:
            v["concedido"] += int((v["pedido"] - v["concedido"]) * fator)

    for v in veiculos:
        if v["bateria"] >= 100:
            v["estado"] = "CHEIO"
        elif v["concedido"] == 0:
            v["estado"] = "ESPERA"
        elif v["concedido"] < v["pedido"]:
            v["estado"] = "REDUZIDA"
        else:
            v["estado"] = "ATIVA"


def statusEstacao(veiculos):
    estados = [v["estado"] for v in veiculos]
    if "ESPERA" in estados:
        return "RECARGA LIMITADA"
    if "REDUZIDA" in estados:
        return "RECARGA CONTROLADA"
    return "RECARGA ATIVA"


# ---------------- saida ----------------
def atualizarLeds(veiculos):
    for v in veiculos:
        verde, amarelo, vermelho = v["leds"]
        verde.value(1 if v["estado"] in ("ATIVA", "CHEIO") else 0)
        amarelo.value(1 if v["estado"] == "REDUZIDA" else 0)
        vermelho.value(1 if v["estado"] == "ESPERA" else 0)


# cada rele so liga se o proprio EV estiver recebendo energia
def atualizarReles(veiculos):
    for v in veiculos:
        v["rele"].value(1 if v["concedido"] > 0 else 0)


def atualizarBaterias(veiculos):
    for v in veiculos:
        wh = v["concedido"] * DT_SIM_S / 3600
        v["energiaWh"] += wh
        v["bateria"] = min(100.0, v["bateria"] + wh / CAPACIDADE_BAT_WH * 100)


def exibirLcd(pagina, nomeCenario, disponivel, status, veiculos):
    if lcd is None:
        return
    if pagina == 0:
        lcd.linha(0, "Disp:" + str(disponivel) + "W " + nomeCenario)
        lcd.linha(1, status)
    else:
        v = veiculos[pagina - 1]
        lcd.linha(0, v["id"] + " " + str(round(v["bateria"])) + "% " + str(v["concedido"]) + "W")
        lcd.linha(1, v["estado"])


def exibirSerial(nomeCenario, disponivel, status, veiculos, entregue, totalWh):
    demanda = sum(v["pedido"] for v in veiculos)
    print("=" * 56)
    print("CENARIO:", nomeCenario, "| STATUS:", status)
    print("ENERGIA DISPONIVEL:", disponivel, "W | DEMANDA TOTAL:", demanda, "W")
    print("  dec:", disponivel, "| hex:", "0x{:04X}".format(disponivel), "| bin:", bin(disponivel)[2:])
    for v in veiculos:
        print("  {} bat:{:5.1f}% pedido:{}W concedido:{}W rele:{} [{}]".format(
            v["id"], v["bateria"], v["pedido"], v["concedido"],
            "ON" if v["rele"].value() else "OFF", v["estado"]))
    aproveit = round(entregue / disponivel * 100) if disponivel > 0 else 0
    kwh = totalWh / 1000
    print("SOLAR ENTREGUE:", entregue, "W (", aproveit, "% do disponivel )")
    print("ENERGIA TOTAL: {:.2f} kWh | CO2 EVITADO: {:.3f} kg".format(kwh, kwh * FATOR_CO2_KG_KWH))


# ---------------- loop principal ----------------
def executar():
    veiculos = criarVeiculos()
    modo = 0
    pagina = 0
    cenarioPress = False
    resetPress = False
    ultimo = time.ticks_add(time.ticks_ms(), -INTERVALO_MS)

    while True:
        clique, cenarioPress = lerClique(botaoCenario, cenarioPress)
        if clique:
            modo = (modo + 1) % len(CENARIOS)
            ultimo = time.ticks_add(time.ticks_ms(), -INTERVALO_MS)   # atualiza ja
            pagina = 0   # mostra o resumo no LCD logo apos a troca
            print("[EVENTO] Cenario:", CENARIOS[modo][0])

        clique, resetPress = lerClique(botaoReset, resetPress)
        if clique:
            veiculos = criarVeiculos()
            ultimo = time.ticks_add(time.ticks_ms(), -INTERVALO_MS)
            pagina = 0
            print("[EVENTO] Baterias reiniciadas (zera tambem o total de kWh e CO2).")

        if time.ticks_diff(time.ticks_ms(), ultimo) >= INTERVALO_MS:
            ultimo = time.ticks_ms()
            nome, fixo = CENARIOS[modo]
            disponivel = lerGeracaoSolar() if fixo is None else fixo

            distribuirEnergia(disponivel, veiculos)
            status = statusEstacao(veiculos)
            atualizarLeds(veiculos)
            atualizarReles(veiculos)
            entregue = sum(v["concedido"] for v in veiculos)

            atualizarBaterias(veiculos)
            totalWh = sum(v["energiaWh"] for v in veiculos)

            exibirSerial(nome, disponivel, status, veiculos, entregue, totalWh)
            exibirLcd(pagina, nome, disponivel, status, veiculos)
            pagina = (pagina + 1) % 4

        time.sleep_ms(30)


executar()
