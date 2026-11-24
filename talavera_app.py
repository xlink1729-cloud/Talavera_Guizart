"""
Control del Taller de Talavera
Inventario de piezas, ventas en tienda, pedidos con anticipo y cuentas del día.
Basada en la app de inventario de Fisioser (Streamlit + PostgreSQL).

Variables de entorno:
  DATABASE_URL    (obligatoria) conexión a PostgreSQL  -> usa una base NUEVA, no la de la clínica
  ADMIN_PASSWORD  (obligatoria) contraseña de acceso
  NEGOCIO_NAME    nombre que aparece en pantalla      (por defecto "Talavera")
  PRIMARY_COLOR   color principal                     (por defecto azul talavera #1e3a8a)
  ZONA_HORARIA    zona para las fechas de "hoy"       (por defecto America/Mexico_City)
  SESION_MINUTOS  minutos de inactividad para cerrar  (por defecto 30)
"""

import base64
import hashlib
import hmac
import io
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import psycopg2
import psycopg2.errors
import streamlit as st

# ==========================================
# 1. CONFIGURACIÓN
# ==========================================
NEGOCIO = os.getenv("NEGOCIO_NAME")
PRIMARY_COLOR = os.getenv("PRIMARY_COLOR")  # azul del logo (tono oscuro)
AZUL_LOGO = "#3680b9"                       # azul del fondo del logo
AZUL_OSCURO = "#1d4f7c"
ACENTO = "#9cc8e8"                          # azul claro para gastos y detalles
ROJO = "#dc2626"
ZONA = ZoneInfo(os.getenv("ZONA_HORARIA"))
SESION_MINUTOS = int(os.getenv("SESION_MINUTOS"))

st.set_page_config(page_title=f"{NEGOCIO} - Control", page_icon="🏺", layout="wide")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not DATABASE_URL or not ADMIN_PASSWORD:
    st.error("🔒 Faltan variables de entorno obligatorias (DATABASE_URL o ADMIN_PASSWORD).")
    st.stop()

CATEGORIAS = ["Platos", "Jarrones", "Macetas", "Tazas y jarras", "Azulejos",
              "Lavabos", "Figuras", "Servicio de mesa", "Otro"]
METODOS_PAGO = ["Efectivo", "Transferencia", "Tarjeta"]
ESTADOS_PEDIDO = ["Pendiente", "En producción", "Listo para entregar", "Entregado", "Cancelado"]
CATEGORIAS_GASTO = ["Barro y materia prima", "Esmaltes y pintura", "Luz (recibo CFE)",
                    "Mantenimiento del horno", "Sueldos", "Renta", "Agua", "Empaque", "Transporte",
                    "Devolución a cliente", "Otro gasto"]
CATEGORIAS_INGRESO_MANUAL = ["Venta sin inventario", "Otro ingreso"]
PERSONALIZADA = "✏️ Pieza personalizada"

st.markdown(
    f"""
    <style>
    .stApp {{ background-color: #f5f9fc; }}
    [data-testid="stSidebar"] {{ background-color: #ffffff; border-right: 3px solid {AZUL_LOGO}; }}
    div.stButton > button[kind="primary"], div.stFormSubmitButton > button {{
        background-color: {PRIMARY_COLOR}; border-color: {PRIMARY_COLOR}; color: #ffffff;
    }}
    div.stButton > button[kind="primary"]:hover, div.stFormSubmitButton > button:hover {{
        background-color: {AZUL_OSCURO}; border-color: {AZUL_OSCURO}; color: #ffffff;
    }}
    .stTabs [aria-selected="true"] {{ color: {PRIMARY_COLOR} !important; }}
    .stTabs [data-baseweb="tab-highlight"] {{ background-color: {PRIMARY_COLOR} !important; }}
    [data-testid="stMetric"], [data-testid="metric-container"] {{
        background-color: #ffffff; border: 1px solid #e2e8f0; padding: 14px;
        border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    h1, h2, h3 {{ color: {PRIMARY_COLOR}; }}
    div.stButton > button, div.stFormSubmitButton > button {{ min-height: 2.8rem; border-radius: 10px; }}
    .franja-talavera {{
        height: 10px; border-radius: 6px; margin: -0.5rem 0 1rem 0;
        background: repeating-linear-gradient(90deg, {AZUL_LOGO} 0 22px, #ffffff 22px 28px,
                    {ACENTO} 28px 34px, #ffffff 34px 40px);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ==========================================
# 2. UTILIDADES
# ==========================================
def hoy() -> date:
    return datetime.now(ZONA).date()


def dinero(x, ocultar: bool = False) -> str:
    return "••••••" if ocultar else f"${(x or 0):,.2f}"


def folio(pid) -> str:
    return f"#{int(pid):04d}"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


ADMIN_PASSWORD_HASH = hash_password(ADMIN_PASSWORD)


def avisar(mensaje: str, tipo: str = "success"):
    """Guarda un mensaje para mostrarlo después del st.rerun()."""
    st.session_state.aviso = (tipo, mensaje)


def mostrar_aviso():
    if "aviso" in st.session_state:
        tipo, mensaje = st.session_state.pop("aviso")
        getattr(st, tipo)(mensaje)


def link_whatsapp(telefono: str, mensaje: str):
    digitos = "".join(c for c in (telefono or "") if c.isdigit())
    if len(digitos) == 10:
        digitos = "52" + digitos
    if len(digitos) < 11:
        return None
    return f"https://wa.me/{digitos}?text={quote(mensaje)}"


DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def texto_pedido_equipo(pedido_id: int, con_precios: bool) -> str:
    """Mensaje listo para mandar al grupo de WhatsApp del personal."""
    p = cargar_pedidos(" WHERE p.id = %s", (pedido_id,)).iloc[0]
    piezas = consultar("SELECT descripcion, cantidad, precio_unitario FROM pedido_detalle"
                       " WHERE pedido_id = %s ORDER BY id", (pedido_id,))
    titulo = (f"NUEVO PEDIDO {folio(pedido_id)}" if p["estado"] == "Pendiente"
              else f"PEDIDO {folio(pedido_id)} · {p['estado'].upper()}")
    lineas = [f"📋 *{titulo}*", f"👤 Cliente: {p['cliente']}"]
    entrega = p["fecha_entrega"]
    if entrega is not None and not pd.isna(entrega):
        dias = (entrega - hoy()).days
        cuando = ("hoy" if dias == 0 else "mañana" if dias == 1 else
                  f"atrasado {-dias} día(s)" if dias < 0 else f"en {dias} días")
        lineas.append(f"📅 Entrega: {DIAS_SEMANA[entrega.weekday()]} {entrega.strftime('%d/%m/%Y')} ({cuando})")
    else:
        lineas.append("📅 Entrega: sin fecha")
    lineas.append("🏺 Piezas:")
    for desc, cant, precio in piezas:
        lineas.append(f"• {cant} × {desc}" + (f" — {dinero(precio)} c/u" if con_precios else ""))
    if p["notas"]:
        lineas.append(f"📝 Notas: {p['notas']}")
    if con_precios:
        lineas.append(f"💰 Total {dinero(p['total'])} · Pagado {dinero(p['pagado'])} · Saldo {dinero(p['saldo'])}")
    return "\n".join(lineas)


def panel_compartir(pedido_id: int, clave: str):
    """Muestra el mensaje del pedido y un botón para mandarlo por WhatsApp (ella elige el grupo)."""
    con_precios = st.checkbox("Incluir precios y saldo en el mensaje", value=False, key=f"precios_{clave}",
                              help="Déjalo sin marcar si el personal no necesita ver cuánto cuesta.")
    mensaje = texto_pedido_equipo(pedido_id, con_precios)
    st.code(mensaje, language=None)
    st.link_button("📤 Compartir por WhatsApp con el equipo", f"https://wa.me/?text={quote(mensaje)}",
                   use_container_width=True, type="primary")
    st.caption("Se abre WhatsApp para que elijas el grupo o la persona. También puedes copiar el texto "
               "con el ícono de la esquina del recuadro.")


def meses_recientes(n: int = 12):
    inicio = hoy().replace(day=1)
    meses = []
    for _ in range(n):
        meses.append(inicio)
        inicio = (inicio - timedelta(days=1)).replace(day=1)
    return meses


def siguiente_mes(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


NOMBRES_MES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def nombre_mes(d: date) -> str:
    return f"{NOMBRES_MES[d.month - 1].capitalize()} {d.year}"


# ==========================================
# 3. BASE DE DATOS
# ==========================================
def get_db():
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def transaccion():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _limpiar(v):
    return float(v) if isinstance(v, Decimal) else v


def consultar(sql, params=None):
    with transaccion() as cur:
        cur.execute(sql, params)
        return [tuple(_limpiar(v) for v in fila) for fila in cur.fetchall()]


def consultar_df(sql, params=None) -> pd.DataFrame:
    with transaccion() as cur:
        cur.execute(sql, params)
        columnas = [d[0] for d in cur.description]
        filas = [tuple(_limpiar(v) for v in fila) for fila in cur.fetchall()]
    return pd.DataFrame(filas, columns=columnas)


def valor(sql, params=None):
    filas = consultar(sql, params)
    return filas[0][0] if filas and filas[0][0] is not None else 0


ESQUEMA = """
CREATE TABLE IF NOT EXISTS productos (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(255) UNIQUE NOT NULL,
    categoria VARCHAR(60) NOT NULL DEFAULT 'Otro',
    medida VARCHAR(60) NOT NULL DEFAULT '',
    diseno VARCHAR(120) NOT NULL DEFAULT '',
    existencias INTEGER NOT NULL DEFAULT 0,
    stock_minimo INTEGER NOT NULL DEFAULT 2,
    costo NUMERIC(12,2) NOT NULL DEFAULT 0,
    precio_venta NUMERIC(12,2) NOT NULL DEFAULT 0,
    precio_mayoreo NUMERIC(12,2) NOT NULL DEFAULT 0,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pedidos (
    id SERIAL PRIMARY KEY,
    cliente VARCHAR(255) NOT NULL,
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    fecha_pedido DATE NOT NULL DEFAULT CURRENT_DATE,
    fecha_entrega DATE,
    estado VARCHAR(30) NOT NULL DEFAULT 'Pendiente',
    total NUMERIC(12,2) NOT NULL DEFAULT 0,
    notas TEXT NOT NULL DEFAULT '',
    inventario_descontado BOOLEAN NOT NULL DEFAULT FALSE,
    creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pedido_detalle (
    id SERIAL PRIMARY KEY,
    pedido_id INTEGER NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
    producto_id INTEGER REFERENCES productos(id) ON DELETE SET NULL,
    descripcion VARCHAR(255) NOT NULL,
    cantidad INTEGER NOT NULL DEFAULT 1,
    precio_unitario NUMERIC(12,2) NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS caja (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    tipo VARCHAR(10) NOT NULL,
    categoria VARCHAR(60) NOT NULL,
    concepto VARCHAR(255) NOT NULL DEFAULT '',
    monto NUMERIC(12,2) NOT NULL,
    metodo VARCHAR(30) NOT NULL DEFAULT 'Efectivo',
    pedido_id INTEGER REFERENCES pedidos(id) ON DELETE SET NULL,
    registrado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS movimientos_inventario (
    id SERIAL PRIMARY KEY,
    producto_id INTEGER REFERENCES productos(id) ON DELETE SET NULL,
    nombre_producto VARCHAR(255) NOT NULL,
    tipo VARCHAR(20) NOT NULL,
    cantidad INTEGER NOT NULL,
    costo_unitario NUMERIC(12,2) NOT NULL DEFAULT 0,
    precio_unitario NUMERIC(12,2) NOT NULL DEFAULT 0,
    nota TEXT NOT NULL DEFAULT '',
    caja_id INTEGER REFERENCES caja(id) ON DELETE SET NULL,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    registrado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_caja_fecha ON caja(fecha);
CREATE INDEX IF NOT EXISTS idx_caja_pedido ON caja(pedido_id);
CREATE INDEX IF NOT EXISTS idx_mov_fecha ON movimientos_inventario(fecha);
"""


TABLAS = ["productos", "pedidos", "pedido_detalle", "caja", "movimientos_inventario"]


@st.cache_resource
def init_db():
    """Crea las tablas solo si faltan. Si ya existen no hace nada, así la app puede conectarse
    con un usuario limitado que no tiene permiso de crear tablas."""
    with transaccion() as cur:
        cur.execute("SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL", (TABLAS,))
        faltantes = [f[0] for f in cur.fetchall()]
    if not faltantes:
        return True
    try:
        with transaccion() as cur:
            cur.execute(ESQUEMA)
    except psycopg2.errors.InsufficientPrivilege:
        st.error(f"🛠️ Falta crear en la base: {', '.join(faltantes)}. El usuario de la app no tiene permiso "
                 "para crear tablas: el administrador debe correr esquema.sql en Neon con el usuario dueño.")
        st.stop()
    return True


init_db()

SQL_PEDIDOS = """
    SELECT p.id, p.cliente, p.telefono, p.fecha_pedido, p.fecha_entrega, p.estado,
           p.total, COALESCE(pg.pagado, 0) AS pagado,
           p.total - COALESCE(pg.pagado, 0) AS saldo,
           p.notas, p.inventario_descontado
    FROM pedidos p
    LEFT JOIN (SELECT pedido_id, SUM(monto) AS pagado FROM caja
               WHERE tipo = 'INGRESO' AND pedido_id IS NOT NULL
               GROUP BY pedido_id) pg ON pg.pedido_id = p.id
"""


def cargar_productos(solo_activos: bool = True) -> pd.DataFrame:
    sql = ("SELECT id, nombre, categoria, medida, diseno, existencias, stock_minimo,"
           " costo, precio_venta, precio_mayoreo, activo FROM productos")
    if solo_activos:
        sql += " WHERE activo"
    return consultar_df(sql + " ORDER BY categoria, nombre")


def cargar_pedidos(condicion: str = "", params=None) -> pd.DataFrame:
    return consultar_df(SQL_PEDIDOS + condicion + " ORDER BY p.fecha_entrega NULLS LAST, p.id", params)


ACENTOS_DE, ACENTOS_A = "áéíóúüñ", "aeiouun"


def _sin_acentos(texto: str) -> str:
    return texto.lower().translate(str.maketrans(ACENTOS_DE, ACENTOS_A))


def buscar_piezas(texto: str, limite: int = 20) -> pd.DataFrame:
    """Busca piezas por nombre, diseño, medida o categoría. Sin importar mayúsculas ni acentos;
    con varias palabras, la pieza debe tener todas (p. ej. «plato azul»)."""
    palabras = [p for p in _sin_acentos(texto).split() if p]
    if not palabras:
        return pd.DataFrame()
    campo = (f"translate(lower(p.nombre || ' ' || p.diseno || ' ' || p.medida || ' ' || p.categoria),"
             f" '{ACENTOS_DE}', '{ACENTOS_A}')")
    condiciones = " AND ".join([f"{campo} LIKE %s ESCAPE '\\'"] * len(palabras))
    patrones = ["%" + p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for p in palabras]
    sql = f"""
        SELECT p.id, p.nombre, p.categoria, p.medida, p.diseno, p.existencias, p.stock_minimo,
               p.costo, p.precio_venta, p.precio_mayoreo, p.activo,
               COALESCE((SELECT SUM(d.cantidad) FROM pedido_detalle d JOIN pedidos pe ON pe.id = d.pedido_id
                         WHERE d.producto_id = p.id AND pe.estado NOT IN ('Entregado', 'Cancelado')
                           AND NOT pe.inventario_descontado), 0) AS en_pedidos,
               COALESCE((SELECT SUM(m.cantidad) FROM movimientos_inventario m
                         WHERE m.producto_id = p.id AND m.tipo IN ('VENTA', 'PEDIDO')
                           AND m.fecha >= %s), 0) AS vendidas_30,
               (SELECT MAX(m.fecha) FROM movimientos_inventario m
                 WHERE m.producto_id = p.id AND m.tipo IN ('VENTA', 'PEDIDO')) AS ultima_venta,
               (SELECT MAX(m.fecha) FROM movimientos_inventario m
                 WHERE m.producto_id = p.id AND m.tipo = 'ENTRADA') AS ultima_entrada
        FROM productos p
        WHERE {condiciones}
        ORDER BY (lower(p.nombre) = lower(%s)) DESC, p.activo DESC, p.nombre
        LIMIT {int(limite)}
    """
    return consultar_df(sql, [hoy() - timedelta(days=30)] + patrones + [texto.strip()])


# ==========================================
# 4. OPERACIONES DEL NEGOCIO
# ==========================================
def _bloquear_producto(cur, producto_id):
    cur.execute("SELECT existencias, costo, nombre FROM productos WHERE id = %s FOR UPDATE",
                (producto_id,))
    fila = cur.fetchone()
    if not fila:
        raise ValueError("La pieza ya no existe en el catálogo.")
    return int(fila[0]), float(fila[1]), fila[2]


def _movimiento(cur, producto_id, nombre, tipo, cantidad, costo, precio, nota, fecha, caja_id=None):
    cur.execute(
        "INSERT INTO movimientos_inventario (producto_id, nombre_producto, tipo, cantidad,"
        " costo_unitario, precio_unitario, nota, caja_id, fecha)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (producto_id, nombre, tipo, cantidad, costo, precio, nota, caja_id, fecha),
    )


def crear_producto(nombre, categoria, medida, diseno, existencias, stock_minimo,
                   costo, precio_venta, precio_mayoreo, fecha):
    with transaccion() as cur:
        cur.execute(
            "INSERT INTO productos (nombre, categoria, medida, diseno, existencias, stock_minimo,"
            " costo, precio_venta, precio_mayoreo) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (nombre, categoria, medida, diseno, existencias, stock_minimo,
             costo, precio_venta, precio_mayoreo),
        )
        pid = cur.fetchone()[0]
        if existencias > 0:
            _movimiento(cur, pid, nombre, "ENTRADA", existencias, costo, 0, "Inventario inicial", fecha)


def entrada_producto(producto_id, cantidad, costo_unitario, fecha, nota):
    with transaccion() as cur:
        existencias, costo_actual, nombre = _bloquear_producto(cur, producto_id)
        if not costo_unitario:
            costo_unitario = costo_actual
        total = existencias + cantidad
        nuevo_costo = ((existencias * costo_actual + cantidad * costo_unitario) / total
                       if total > 0 else costo_unitario)
        cur.execute("UPDATE productos SET existencias = existencias + %s, costo = %s WHERE id = %s",
                    (cantidad, round(nuevo_costo, 2), producto_id))
        _movimiento(cur, producto_id, nombre, "ENTRADA", cantidad, costo_unitario, 0, nota, fecha)


def merma_producto(producto_id, cantidad, fecha, nota):
    with transaccion() as cur:
        existencias, costo, nombre = _bloquear_producto(cur, producto_id)
        if cantidad > existencias:
            raise ValueError(f"Solo hay {existencias} de «{nombre}».")
        cur.execute("UPDATE productos SET existencias = existencias - %s WHERE id = %s",
                    (cantidad, producto_id))
        _movimiento(cur, producto_id, nombre, "MERMA", cantidad, costo, 0, nota, fecha)


def corregir_conteo(producto_id, cantidad_real, fecha, nota):
    with transaccion() as cur:
        existencias, costo, nombre = _bloquear_producto(cur, producto_id)
        diferencia = cantidad_real - existencias
        if diferencia == 0:
            raise ValueError("El conteo coincide con el sistema, no hay nada que corregir.")
        cur.execute("UPDATE productos SET existencias = %s WHERE id = %s", (cantidad_real, producto_id))
        _movimiento(cur, producto_id, nombre, "AJUSTE", diferencia, costo, 0,
                    nota or f"Conteo físico: había {existencias}, hay {cantidad_real}", fecha)


def registrar_venta(lineas, metodo, fecha, nota):
    total = sum(l["subtotal"] for l in lineas)
    concepto = ", ".join(f"{l['cantidad']}× {l['nombre']}" for l in lineas)
    if nota:
        concepto = f"{concepto} ({nota})"
    with transaccion() as cur:
        cur.execute(
            "INSERT INTO caja (fecha, tipo, categoria, concepto, monto, metodo)"
            " VALUES (%s, 'INGRESO', 'Venta mostrador', %s, %s, %s) RETURNING id",
            (fecha, concepto[:255], total, metodo),
        )
        caja_id = cur.fetchone()[0]
        for l in lineas:
            existencias, costo, nombre = _bloquear_producto(cur, l["producto_id"])
            if l["cantidad"] > existencias:
                raise ValueError(f"No alcanzan las piezas de «{nombre}»: quedan {existencias}.")
            cur.execute("UPDATE productos SET existencias = existencias - %s WHERE id = %s",
                        (l["cantidad"], l["producto_id"]))
            _movimiento(cur, l["producto_id"], nombre, "VENTA", l["cantidad"], costo,
                        l["precio"], nota, fecha, caja_id)
    return total


def crear_pedido(cliente, telefono, fecha_pedido, fecha_entrega, notas, lineas, anticipo, metodo):
    total = sum(l["subtotal"] for l in lineas)
    with transaccion() as cur:
        cur.execute(
            "INSERT INTO pedidos (cliente, telefono, fecha_pedido, fecha_entrega, total, notas)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (cliente, telefono, fecha_pedido, fecha_entrega, total, notas),
        )
        pid = cur.fetchone()[0]
        for l in lineas:
            cur.execute(
                "INSERT INTO pedido_detalle (pedido_id, producto_id, descripcion, cantidad, precio_unitario)"
                " VALUES (%s, %s, %s, %s, %s)",
                (pid, l["producto_id"], l["descripcion"][:255], l["cantidad"], l["precio"]),
            )
        if anticipo > 0:
            cur.execute(
                "INSERT INTO caja (fecha, tipo, categoria, concepto, monto, metodo, pedido_id)"
                " VALUES (%s, 'INGRESO', 'Anticipo de pedido', %s, %s, %s, %s)",
                (fecha_pedido, f"Pedido {folio(pid)} - {cliente}"[:255], anticipo, metodo, pid),
            )
    return pid


def registrar_abono(pedido_id, cliente, monto, metodo, fecha):
    with transaccion() as cur:
        cur.execute(
            "INSERT INTO caja (fecha, tipo, categoria, concepto, monto, metodo, pedido_id)"
            " VALUES (%s, 'INGRESO', 'Abono / liquidación de pedido', %s, %s, %s, %s)",
            (fecha, f"Pedido {folio(pedido_id)} - {cliente}"[:255], monto, metodo, pedido_id),
        )


def cambiar_estado_pedido(pedido_id, estado, descontar, fecha):
    with transaccion() as cur:
        cur.execute("SELECT inventario_descontado FROM pedidos WHERE id = %s FOR UPDATE", (pedido_id,))
        ya_descontado = cur.fetchone()[0]
        if estado == "Entregado" and descontar and not ya_descontado:
            cur.execute(
                "SELECT producto_id, cantidad, precio_unitario FROM pedido_detalle"
                " WHERE pedido_id = %s AND producto_id IS NOT NULL ORDER BY id",
                (pedido_id,),
            )
            for producto_id, cantidad, precio in cur.fetchall():
                existencias, costo, nombre = _bloquear_producto(cur, producto_id)
                if cantidad > existencias:
                    raise ValueError(
                        f"No hay suficientes «{nombre}» en inventario (quedan {existencias}, el pedido"
                        f" lleva {cantidad}). Registra primero la entrada de piezas o desmarca"
                        " «Descontar del inventario».")
                cur.execute("UPDATE productos SET existencias = existencias - %s WHERE id = %s",
                            (cantidad, producto_id))
                _movimiento(cur, producto_id, nombre, "PEDIDO", cantidad, costo, precio,
                            f"Pedido {folio(pedido_id)}", fecha)
            cur.execute("UPDATE pedidos SET inventario_descontado = TRUE WHERE id = %s", (pedido_id,))
        cur.execute("UPDATE pedidos SET estado = %s WHERE id = %s", (estado, pedido_id))


def editar_pedido(pedido_id, cliente, telefono, fecha_entrega, notas, lineas=None):
    """Cambia los datos del pedido. Si se pasan líneas, reemplaza las piezas y recalcula el total."""
    with transaccion() as cur:
        cur.execute("SELECT inventario_descontado FROM pedidos WHERE id = %s FOR UPDATE", (pedido_id,))
        descontado = cur.fetchone()[0]
        cur.execute("UPDATE pedidos SET cliente = %s, telefono = %s, fecha_entrega = %s, notas = %s WHERE id = %s",
                    (cliente, telefono, fecha_entrega, notas, pedido_id))
        if lineas is not None:
            if descontado:
                raise ValueError("Las piezas de este pedido ya se descontaron del inventario; ya no se pueden cambiar.")
            cur.execute("DELETE FROM pedido_detalle WHERE pedido_id = %s", (pedido_id,))
            for l in lineas:
                cur.execute(
                    "INSERT INTO pedido_detalle (pedido_id, producto_id, descripcion, cantidad, precio_unitario)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (pedido_id, l["producto_id"], l["descripcion"][:255], l["cantidad"], l["precio"]),
                )
            cur.execute("UPDATE pedidos SET total = %s WHERE id = %s",
                        (sum(l["subtotal"] for l in lineas), pedido_id))


def registrar_caja(fecha, tipo, categoria, concepto, monto, metodo):
    with transaccion() as cur:
        cur.execute(
            "INSERT INTO caja (fecha, tipo, categoria, concepto, monto, metodo)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (fecha, tipo, categoria, concepto[:255], monto, metodo),
        )


def eliminar_caja(caja_id):
    """Borra un movimiento de dinero. Si era una venta, regresa las piezas al inventario."""
    with transaccion() as cur:
        cur.execute(
            "SELECT producto_id, cantidad FROM movimientos_inventario"
            " WHERE caja_id = %s AND tipo = 'VENTA'", (caja_id,))
        for producto_id, cantidad in cur.fetchall():
            if producto_id is not None:
                cur.execute("UPDATE productos SET existencias = existencias + %s WHERE id = %s",
                            (cantidad, producto_id))
        cur.execute("DELETE FROM movimientos_inventario WHERE caja_id = %s AND tipo = 'VENTA'", (caja_id,))
        cur.execute("DELETE FROM caja WHERE id = %s", (caja_id,))


# ==========================================
# 5. CAPTURA DE PIEZAS (ventas y pedidos)
# ==========================================
def tabla_vacia(con_descripcion: bool) -> pd.DataFrame:
    datos = {"Pieza": pd.Series([None], dtype="object")}
    if con_descripcion:
        datos["Descripción"] = pd.Series([""], dtype="object")
    datos["Cantidad"] = pd.Series([1], dtype="int64")
    datos["Precio c/u"] = pd.Series([None], dtype="float64")
    return pd.DataFrame(datos)


def armar_lineas(df: pd.DataFrame, mapa: dict, mayoreo: bool = False):
    """Convierte lo capturado en la tabla en líneas con precio. Precio vacío = precio de lista."""
    lineas, avisos = [], []
    for _, r in df.iterrows():
        pieza = r.get("Pieza")
        pieza = pieza if isinstance(pieza, str) and pieza else None
        desc = r.get("Descripción")
        desc = desc.strip() if isinstance(desc, str) else ""
        cantidad = r.get("Cantidad")
        precio = r.get("Precio c/u")
        if pieza is None and not desc:
            continue
        if cantidad is None or pd.isna(cantidad) or int(cantidad) <= 0:
            avisos.append(f"Falta la cantidad de «{desc or pieza}».")
            continue
        if pieza and pieza != PERSONALIZADA and pieza in mapa:
            p = mapa[pieza]
            lista = p["precio_mayoreo"] if mayoreo and p["precio_mayoreo"] > 0 else p["precio_venta"]
            linea = {"producto_id": int(p["id"]), "nombre": pieza, "descripcion": desc or pieza,
                     "existencias": int(p["existencias"])}
        else:
            if not desc:
                avisos.append("Escribe en «Descripción» cómo es la pieza personalizada.")
                continue
            lista = 0.0
            linea = {"producto_id": None, "nombre": desc, "descripcion": desc, "existencias": None}
        linea["cantidad"] = int(cantidad)
        linea["precio"] = float(precio) if precio is not None and not pd.isna(precio) else float(lista)
        linea["subtotal"] = linea["cantidad"] * linea["precio"]
        if linea["precio"] <= 0:
            avisos.append(f"«{linea['nombre']}» no tiene precio.")
        lineas.append(linea)
    return lineas, avisos


def resumen_lineas(lineas):
    df = pd.DataFrame([{"Pieza": l["descripcion"], "Cantidad": l["cantidad"],
                        "Precio c/u": l["precio"], "Subtotal": l["subtotal"]} for l in lineas])
    st.dataframe(df, hide_index=True, use_container_width=True, column_config={
        "Precio c/u": st.column_config.NumberColumn(format="$%.2f"),
        "Subtotal": st.column_config.NumberColumn(format="$%.2f"),
    })


DINERO_COL = st.column_config.NumberColumn(format="$%.2f")


# ==========================================
# 5b. SEGURIDAD: LÍMITE DE INTENTOS DE ACCESO
# ==========================================
MAX_INTENTOS = 5            # fallos seguidos antes de bloquear a ese dispositivo
BLOQUEO_BASE_MIN = 5        # primer bloqueo: 5 min; luego 10, 20, 40... hasta 60
BLOQUEO_MAX_MIN = 60
LIMITE_GLOBAL = 25          # si hay 25 fallos en total en 15 min, se pausa el acceso para todos
VENTANA_GLOBAL_SEG = 15 * 60


@st.cache_resource
def _registro_intentos():
    """Compartido entre todas las sesiones del servidor (no se borra al recargar la página)."""
    return {"candado": threading.Lock(), "clientes": {}, "fallos_globales": []}


def _clave_cliente() -> str:
    try:
        h = st.context.headers
        return (h.get("X-Forwarded-For") or h.get("X-Real-Ip") or "local").strip()
    except Exception:
        return "local"


def segundos_bloqueo(clave: str) -> int:
    reg = _registro_intentos()
    ahora = time.time()
    with reg["candado"]:
        reg["fallos_globales"] = [t for t in reg["fallos_globales"] if ahora - t < VENTANA_GLOBAL_SEG]
        restante = reg["clientes"].get(clave, {}).get("bloqueado_hasta", 0) - ahora
        if len(reg["fallos_globales"]) >= LIMITE_GLOBAL:
            restante = max(restante, reg["fallos_globales"][0] + VENTANA_GLOBAL_SEG - ahora)
    return max(0, int(restante))


def registrar_fallo(clave: str) -> int:
    """Anota un intento fallido. Regresa cuántos intentos le quedan antes del bloqueo."""
    reg = _registro_intentos()
    ahora = time.time()
    with reg["candado"]:
        reg["fallos_globales"].append(ahora)
        c = reg["clientes"].setdefault(clave, {"fallos": 0, "bloqueos": 0, "bloqueado_hasta": 0})
        c["fallos"] += 1
        if c["fallos"] >= MAX_INTENTOS:
            minutos = min(BLOQUEO_BASE_MIN * 2 ** c["bloqueos"], BLOQUEO_MAX_MIN)
            c["bloqueado_hasta"] = ahora + minutos * 60
            c["bloqueos"] += 1
            c["fallos"] = 0
            return 0
        return MAX_INTENTOS - c["fallos"]


def registrar_exito(clave: str):
    reg = _registro_intentos()
    with reg["candado"]:
        reg["clientes"].pop(clave, None)


def texto_espera(segundos: int) -> str:
    minutos = (segundos + 59) // 60
    return f"{minutos} minuto{'s' if minutos != 1 else ''}"


# ==========================================
# 5c. RESPALDO EN EXCEL
# ==========================================
HOJAS_RESPALDO = {
    "Piezas": "SELECT id, nombre, categoria, medida, diseno, existencias, stock_minimo, costo,"
              " precio_venta, precio_mayoreo, activo, creado FROM productos ORDER BY id",
    "Pedidos": SQL_PEDIDOS + " ORDER BY p.id",
    "Detalle de pedidos": "SELECT d.pedido_id, p.cliente, d.producto_id, d.descripcion, d.cantidad,"
                          " d.precio_unitario, d.cantidad * d.precio_unitario AS subtotal"
                          " FROM pedido_detalle d JOIN pedidos p ON p.id = d.pedido_id ORDER BY d.pedido_id, d.id",
    "Caja": "SELECT id, fecha, tipo, categoria, concepto, monto, metodo, pedido_id, registrado"
            " FROM caja ORDER BY fecha, id",
    "Movimientos de piezas": "SELECT id, fecha, tipo, producto_id, nombre_producto, cantidad, costo_unitario,"
                             " precio_unitario, nota, caja_id, registrado FROM movimientos_inventario"
                             " ORDER BY fecha, id",
}


def generar_respaldo() -> bytes:
    salida = io.BytesIO()
    with pd.ExcelWriter(salida, engine="openpyxl") as excel:
        for hoja, sql in HOJAS_RESPALDO.items():
            df = consultar_df(sql)
            df.to_excel(excel, sheet_name=hoja, index=False)
            ws = excel.sheets[hoja]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, start=1):
                largo = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(10, largo + 2), 45)
    return salida.getvalue()

# ==========================================
# 6. ACCESO
# ==========================================
for clave, inicial in {"autenticado": False, "ultimo_acceso": None, "ocultar_cifras": False}.items():
    if clave not in st.session_state:
        st.session_state[clave] = inicial

if st.session_state.autenticado and st.session_state.ultimo_acceso:
    if datetime.now() - st.session_state.ultimo_acceso > timedelta(minutes=SESION_MINUTOS):
        st.session_state.clear()
        st.session_state.autenticado = False
        avisar("⏳ La sesión se cerró por inactividad. Vuelve a entrar.", "warning")
        st.rerun()
if st.session_state.autenticado:
    st.session_state.ultimo_acceso = datetime.now()

CARPETA_APP = os.path.dirname(os.path.abspath(__file__))
LOGO = next((os.path.join(CARPETA_APP, "static", f"logo.{ext}")
             for ext in ("jpg", "jpeg", "png", "webp")
             if os.path.exists(os.path.join(CARPETA_APP, "static", f"logo.{ext}"))), "")
LOGO_MIME = {"png": "image/png", "webp": "image/webp"}.get(LOGO.rsplit(".", 1)[-1], "image/jpeg")

if not st.session_state.autenticado:
    st.markdown(
        f"""
        <style>
        #MainMenu {{visibility: hidden;}} footer {{visibility: hidden;}}
        .stApp {{ background: linear-gradient(180deg, #ffffff 0%, #eef5fb 100%); }}
        div.block-container {{ padding-top: 3rem; }}
        .logo-login {{
            width: 170px; max-width: 55vw; border-radius: 24px; border: 6px solid #ffffff;
            box-shadow: 0 14px 34px rgba(29, 79, 124, 0.35);
        }}
        div[data-testid="stForm"] {{
            background: #ffffff !important; border-radius: 18px !important;
            padding: 1.8rem !important; box-shadow: 0 8px 24px rgba(29, 79, 124, 0.12) !important;
            border: 1px solid #dbe8f3 !important;
        }}
        div.stFormSubmitButton > button {{
            background: {PRIMARY_COLOR} !important; color: #fff !important; border: none !important;
            font-weight: 600 !important;
        }}
        div.stFormSubmitButton > button * {{ color: #fff !important; }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    _, centro, _ = st.columns([1, 1.1, 1])
    with centro:
        mostrar_aviso()
        if LOGO:
            with open(LOGO, "rb") as f:
                logo64 = base64.b64encode(f.read()).decode("utf-8")
            st.markdown(
                f"<div style='display:flex;justify-content:center;margin-bottom:1.2rem;'>"
                f"<img class='logo-login' src='data:{LOGO_MIME};base64,{logo64}'></div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"<h1 style='text-align:center;color:{AZUL_OSCURO};font-size:1.9rem;margin-bottom:0.2rem;'>"
            f"{NEGOCIO}</h1>"
            "<p style='text-align:center;color:#5b7a95;margin-bottom:1.3rem;'>Control del taller</p>",
            unsafe_allow_html=True,
        )
        with st.form("login_form"):
            pwd = st.text_input("Contraseña", type="password", placeholder="••••••••",
                                label_visibility="collapsed")
            if st.form_submit_button("Entrar", use_container_width=True):
                cliente_login = _clave_cliente()
                espera = segundos_bloqueo(cliente_login)
                if espera > 0:
                    st.error(f"🔒 Demasiados intentos fallidos. Espera {texto_espera(espera)} y vuelve a intentar.")
                elif hmac.compare_digest(hash_password(pwd), ADMIN_PASSWORD_HASH):
                    registrar_exito(cliente_login)
                    st.session_state.autenticado = True
                    st.session_state.ultimo_acceso = datetime.now()
                    st.rerun()
                else:
                    time.sleep(1)  # frena a quien intente adivinar en automático
                    quedan = registrar_fallo(cliente_login)
                    if quedan == 0:
                        st.error("🔒 Demasiados intentos fallidos. El acceso se bloqueó unos minutos.")
                    elif quedan <= 2:
                        st.error(f"❌ Contraseña incorrecta. Te queda{'n' if quedan > 1 else ''} {quedan} "
                                 f"intento{'s' if quedan > 1 else ''}.")
                    else:
                        st.error("❌ Contraseña incorrecta.")
    st.stop()

# ==========================================
# 7. BARRA LATERAL
# ==========================================
with st.sidebar:
    if LOGO:
        st.image(LOGO, width=110)
    st.markdown(f"### 🏺 {NEGOCIO}")
    st.toggle("🙈 Ocultar cifras", key="ocultar_cifras")
    st.markdown("---")
    st.markdown("**💾 Respaldo**")
    st.caption("Descarga un Excel con todo (piezas, pedidos y caja). Guárdalo cada semana.")
    if st.button("Preparar respaldo", use_container_width=True):
        with st.spinner("Preparando..."):
            st.session_state.respaldo = (generar_respaldo(), datetime.now(ZONA).strftime("%Y-%m-%d_%H%M"))
    if "respaldo" in st.session_state:
        datos_respaldo, sello = st.session_state.respaldo
        st.download_button("⬇️ Descargar Excel", data=datos_respaldo, file_name=f"respaldo_talavera_{sello}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, type="primary")
    st.markdown("---")
    if st.button("🚪 Cerrar sesión", use_container_width=True):
        st.session_state.clear()
        st.rerun()

OCULTAR = st.session_state.ocultar_cifras

st.title(f"🏺 {NEGOCIO}")
st.markdown("<div class='franja-talavera'></div>", unsafe_allow_html=True)
mostrar_aviso()

tab_resumen, tab_buscar, tab_vender, tab_pedidos, tab_inventario, tab_cuentas = st.tabs(
    ["📊 Resumen", "🔎 Buscar", "🛒 Vender", "📋 Pedidos", "🏺 Inventario", "💰 Cuentas"])

# ------------------------------------------
# TAB: RESUMEN
# ------------------------------------------
with tab_resumen:
    h = hoy()
    inicio_mes = h.replace(day=1)
    ingresos_hoy = valor("SELECT SUM(monto) FROM caja WHERE tipo = 'INGRESO' AND fecha = %s", (h,))
    ingresos_mes = valor("SELECT SUM(monto) FROM caja WHERE tipo = 'INGRESO' AND fecha >= %s", (inicio_mes,))
    gastos_mes = valor("SELECT SUM(monto) FROM caja WHERE tipo = 'GASTO' AND fecha >= %s", (inicio_mes,))
    ganancia_piezas = valor(
        "SELECT SUM((precio_unitario - costo_unitario) * cantidad) FROM movimientos_inventario"
        " WHERE tipo IN ('VENTA', 'PEDIDO') AND fecha >= %s", (inicio_mes,))

    st.subheader(f"Hoy, {h.day} de {NOMBRES_MES[h.month - 1]}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💵 Entró hoy", dinero(ingresos_hoy, OCULTAR))
    c2.metric(f"📈 Ingresos de {NOMBRES_MES[h.month - 1]}", dinero(ingresos_mes, OCULTAR))
    c3.metric(f"📉 Gastos de {NOMBRES_MES[h.month - 1]}", dinero(gastos_mes, OCULTAR))
    c4.metric("⚖️ Balance del mes", dinero(ingresos_mes - gastos_mes, OCULTAR),
              help="Todo lo que entró menos todo lo que salió este mes.")

    pedidos_vivos = cargar_pedidos(" WHERE p.estado <> 'Cancelado'")
    activos = pedidos_vivos[~pedidos_vivos["estado"].isin(["Entregado"])] if not pedidos_vivos.empty else pedidos_vivos
    por_cobrar = pedidos_vivos["saldo"].clip(lower=0).sum() if not pedidos_vivos.empty else 0
    if not activos.empty:
        con_fecha = activos[activos["fecha_entrega"].notna()]
        atrasados = con_fecha[con_fecha["fecha_entrega"] < h]
        proximos = con_fecha[(con_fecha["fecha_entrega"] >= h) & (con_fecha["fecha_entrega"] <= h + timedelta(days=7))]
    else:
        atrasados = proximos = activos

    prods = cargar_productos()
    bajos = prods[prods["existencias"] <= prods["stock_minimo"]] if not prods.empty else prods

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 Pedidos activos", len(activos))
    c2.metric("🚚 Entregas en 7 días", len(proximos),
              delta=f"{len(atrasados)} atrasados" if len(atrasados) else None, delta_color="inverse")
    c3.metric("🧾 Por cobrar en pedidos", dinero(por_cobrar, OCULTAR))
    c4.metric("⚠️ Piezas por acabarse", len(bajos))

    st.markdown("---")
    izq, der = st.columns(2)
    with izq:
        st.subheader("🚚 Próximas entregas")
        lista = pd.concat([atrasados, proximos]) if not activos.empty else activos
        if lista.empty:
            st.success("No hay entregas pendientes para esta semana. 🎉")
        else:
            vista = pd.DataFrame({
                "Folio": lista["id"].map(folio),
                "Cliente": lista["cliente"],
                "Entrega": lista["fecha_entrega"],
                "Estado": lista["estado"],
                "Saldo": lista["saldo"],
            })
            st.dataframe(vista, hide_index=True, use_container_width=True,
                         column_config={"Saldo": DINERO_COL})
            if len(atrasados):
                st.error(f"⏰ Hay {len(atrasados)} pedido(s) con fecha de entrega vencida.")
    with der:
        st.subheader("⚠️ Piezas por acabarse")
        if bajos.empty:
            st.success("Todo el inventario está por encima del mínimo.")
        else:
            for _, r in bajos.iterrows():
                st.markdown(f"- **{r['nombre']}**: quedan {r['existencias']} (mínimo {r['stock_minimo']})")

    st.markdown("---")
    izq, der = st.columns(2)
    with izq:
        st.subheader("🏆 Lo más vendido (30 días)")
        top = consultar_df(
            "SELECT nombre_producto AS pieza, SUM(cantidad) AS piezas FROM movimientos_inventario"
            " WHERE tipo IN ('VENTA', 'PEDIDO') AND fecha >= %s"
            " GROUP BY nombre_producto ORDER BY piezas DESC LIMIT 5", (h - timedelta(days=30),))
        if top.empty:
            st.info("Aún no hay ventas registradas.")
        else:
            fig = px.bar(top, x="piezas", y="pieza", orientation="h", text="piezas",
                         color_discrete_sequence=[PRIMARY_COLOR])
            fig.update_layout(height=260, margin=dict(t=10, b=10, l=10, r=10), yaxis_title=None,
                              xaxis_title="Piezas", yaxis=dict(autorange="reversed"),
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)
    with der:
        st.subheader("💰 Entradas y salidas de dinero (14 días)")
        if OCULTAR:
            st.info("Cifras ocultas.")
        else:
            diario = consultar_df(
                "SELECT fecha, tipo, SUM(monto) AS monto FROM caja WHERE fecha >= %s"
                " GROUP BY fecha, tipo ORDER BY fecha", (h - timedelta(days=13),))
            if diario.empty:
                st.info("Aún no hay movimientos de dinero.")
            else:
                diario["tipo"] = diario["tipo"].map({"INGRESO": "Ingresos", "GASTO": "Gastos"})
                fig = px.bar(diario, x="fecha", y="monto", color="tipo", barmode="group",
                             color_discrete_map={"Ingresos": PRIMARY_COLOR, "Gastos": ACENTO})
                fig.update_layout(height=260, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None,
                                  yaxis_title=None, legend_title=None,
                                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, use_container_width=True)

    if not prods.empty:
        valor_costo = (prods["existencias"] * prods["costo"]).sum()
        valor_venta = (prods["existencias"] * prods["precio_venta"]).sum()
        st.caption(
            f"🏺 Inventario en tienda: **{int(prods['existencias'].sum())} piezas** · "
            f"valor a costo {dinero(valor_costo, OCULTAR)} · a precio de venta {dinero(valor_venta, OCULTAR)} · "
            f"ganancia en piezas vendidas este mes {dinero(ganancia_piezas, OCULTAR)}"
        )

# ------------------------------------------
# TAB: BUSCAR PIEZA
# ------------------------------------------
def _fecha_txt(f) -> str:
    return f.strftime("%d/%m/%Y") if f is not None and not pd.isna(f) else "—"


def ficha_pieza(r):
    """Muestra todos los datos de una pieza encontrada."""
    hay, minimo, en_ped = int(r["existencias"]), int(r["stock_minimo"]), int(r["en_pedidos"])
    if not r["activo"]:
        estado = "⚫ Inactiva (ya no se vende)"
    elif hay == 0:
        estado = "🔴 Agotada"
    elif hay <= minimo:
        estado = "🟡 Por acabarse"
    else:
        estado = "🟢 Disponible"

    with st.container(border=True):
        st.markdown(f"### 🏺 {r['nombre']}")
        detalles = [f"**Categoría:** {r['categoria']}"]
        if r["medida"]:
            detalles.append(f"**Medida:** {r['medida']}")
        if r["diseno"]:
            detalles.append(f"**Diseño:** {r['diseno']}")
        st.markdown(" · ".join(detalles) + f" · {estado}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📦 Hay en inventario", hay, help=f"Te avisa cuando queden {minimo} o menos.")
        c2.metric("📋 En pedidos por entregar", en_ped,
                  help="Piezas de este modelo en pedidos que aún no se entregan.")
        libres = hay - en_ped
        c3.metric("✅ Libres para vender", max(libres, 0),
                  delta=f"faltan {-libres}" if libres < 0 else None, delta_color="inverse",
                  help="Las que hay menos las que ya están comprometidas en pedidos.")
        c4.metric("🛒 Vendidas (30 días)", int(r["vendidas_30"]))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🏷️ Precio de venta", dinero(r["precio_venta"]))
        c2.metric("📦 Precio de mayoreo", dinero(r["precio_mayoreo"]) if r["precio_mayoreo"] > 0 else "—")
        c3.metric("💲 Costo", dinero(r["costo"], OCULTAR))
        c4.metric("💰 Ganancia por pieza", dinero(r["precio_venta"] - r["costo"], OCULTAR),
                  help="Precio de venta menos costo.")
        st.caption(f"Última venta: {_fecha_txt(r['ultima_venta'])} · "
                   f"Última entrada de piezas: {_fecha_txt(r['ultima_entrada'])}")

        pedidos_pieza = consultar_df(
            "SELECT pe.id, pe.cliente, pe.fecha_entrega, pe.estado, d.cantidad FROM pedido_detalle d"
            " JOIN pedidos pe ON pe.id = d.pedido_id WHERE d.producto_id = %s"
            " AND pe.estado NOT IN ('Entregado', 'Cancelado') AND NOT pe.inventario_descontado"
            " ORDER BY pe.fecha_entrega NULLS LAST", (int(r["id"]),))
        movs = consultar_df(
            'SELECT fecha AS "Fecha", tipo AS "Tipo", cantidad AS "Cantidad", nota AS "Nota"'
            " FROM movimientos_inventario WHERE producto_id = %s ORDER BY fecha DESC, id DESC LIMIT 10",
            (int(r["id"]),))
        if not pedidos_pieza.empty:
            with st.expander(f"📋 Pedidos que la llevan ({len(pedidos_pieza)})"):
                st.dataframe(pd.DataFrame({
                    "Folio": pedidos_pieza["id"].map(folio), "Cliente": pedidos_pieza["cliente"],
                    "Piezas": pedidos_pieza["cantidad"], "Entrega": pedidos_pieza["fecha_entrega"],
                    "Estado": pedidos_pieza["estado"],
                }), hide_index=True, use_container_width=True)
        if not movs.empty:
            with st.expander("📅 Últimos movimientos"):
                st.dataframe(movs, hide_index=True, use_container_width=True)


with tab_buscar:
    st.subheader("🔎 Buscar pieza")
    texto_busqueda = st.text_input(
        "¿Qué pieza buscas?", key="buscar_texto", placeholder="Ej. plato pozolero, jarrón, azul, 25 cm",
        help="Escribe el nombre o parte de él. También busca por diseño, medida y categoría. "
             "No importan mayúsculas ni acentos; con varias palabras muestra las piezas que tengan todas.")
    if texto_busqueda.strip():
        encontradas = buscar_piezas(texto_busqueda)
        if encontradas.empty:
            st.warning(f"No hay ninguna pieza que coincida con «{texto_busqueda.strip()}». "
                       "Revisa cómo se escribe o búscala con menos palabras. Si es nueva, dala de alta "
                       "en 🏺 Inventario.")
        else:
            n = len(encontradas)
            st.caption(f"{n} pieza{'s' if n != 1 else ''} encontrada{'s' if n != 1 else ''}"
                       + (" (se muestran las primeras 20; escribe algo más específico)" if n >= 20 else ""))
            for _, fila_pieza in encontradas.iterrows():
                ficha_pieza(fila_pieza)
    else:
        st.caption("Escribe arriba y presiona Enter.")

# ------------------------------------------
# TAB: VENDER
# ------------------------------------------
with tab_vender:
    st.subheader("🛒 Venta en tienda")
    prods = cargar_productos()
    if prods.empty:
        st.info("Primero da de alta tus piezas en la pestaña 🏺 Inventario.")
    else:
        mapa = {r["nombre"]: r for r in prods.to_dict("records")}
        c1, c2, c3 = st.columns(3)
        fecha_v = c1.date_input("Fecha", value=hoy(), key="venta_fecha", format="DD/MM/YYYY",
                                help="Día de la venta. Cámbialo solo si estás anotando una venta de otro día.")
        metodo_v = c2.selectbox("Forma de pago", METODOS_PAGO, key="venta_metodo",
                                help="Cómo pagó el cliente. Sirve para que cuadre el corte de caja.")
        mayoreo = c3.toggle("Precio de mayoreo", key="venta_mayoreo",
                            help="Actívalo si el cliente compra al mayoreo: se usa el precio de mayoreo de cada "
                                 "pieza (si no tiene, se usa el normal).")

        st.caption("Agrega una fila por pieza. Si dejas el precio vacío se usa el precio de lista.")
        version = st.session_state.get("venta_version", 0)
        capturado = st.data_editor(
            tabla_vacia(con_descripcion=False), key=f"venta_editor_{version}", num_rows="dynamic",
            hide_index=True, use_container_width=True,
            column_config={
                "Pieza": st.column_config.SelectboxColumn("Pieza", options=list(mapa.keys()), width="large",
                                                          help="Toca la celda y elige la pieza del catálogo."),
                "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=1, step=1, default=1,
                                                          help="Cuántas piezas se lleva."),
                "Precio c/u": st.column_config.NumberColumn(
                    "Precio c/u", min_value=0.0, step=1.0, format="$%.2f",
                    help="Déjalo vacío para usar el precio de lista. Escríbelo solo si hiciste descuento."),
            },
        )
        lineas, avisos = armar_lineas(capturado, mapa, mayoreo)

        faltantes = {}
        for l in lineas:
            faltantes[l["nombre"]] = faltantes.get(l["nombre"], 0) + l["cantidad"]
        for nombre, cant in faltantes.items():
            if cant > mapa[nombre]["existencias"]:
                avisos.append(f"Solo hay {mapa[nombre]['existencias']} de «{nombre}» y quieres vender {cant}.")
        for a in avisos:
            st.warning(a)

        if lineas:
            total_v = sum(l["subtotal"] for l in lineas)
            resumen_lineas(lineas)
            st.markdown(f"## Total: {dinero(total_v)}")
        nota_v = st.text_input("Nota (opcional)", key=f"venta_nota_{version}",
                               placeholder="Ej. cliente de Puebla, se llevó envuelto",
                               help="Cualquier dato que quieras recordar de esta venta. Aparece en el corte de caja.")
        col_ok, col_cancel = st.columns([2, 1])
        if col_cancel.button("❌ Cancelar venta", use_container_width=True, key=f"venta_cancelar_{version}",
                             help="Borra lo que capturaste sin registrar nada."):
            st.session_state.venta_version = version + 1
            avisar("Venta cancelada, no se registró nada.", "info")
            st.rerun()
        if col_ok.button("✅ Registrar venta", type="primary", use_container_width=True,
                         disabled=not lineas or bool(avisos),
                         help="Descuenta las piezas del inventario y anota el cobro en caja."):
            try:
                total_v = registrar_venta(lineas, metodo_v, fecha_v, nota_v.strip())
                st.session_state.venta_version = version + 1
                avisar(f"✅ Venta registrada por {dinero(total_v)}. El inventario ya se actualizó.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

        with st.expander("📦 Ver existencias y precios"):
            st.dataframe(
                prods[["nombre", "categoria", "existencias", "precio_venta", "precio_mayoreo"]].rename(
                    columns={"nombre": "Pieza", "categoria": "Categoría", "existencias": "Hay",
                             "precio_venta": "Precio", "precio_mayoreo": "Mayoreo"}),
                hide_index=True, use_container_width=True,
                column_config={"Precio": DINERO_COL, "Mayoreo": DINERO_COL})

# ------------------------------------------
# TAB: PEDIDOS
# ------------------------------------------
with tab_pedidos:
    sub_nuevo, sub_activos, sub_cobrar, sub_historial = st.tabs(
        ["➕ Nuevo pedido", "📋 Activos", "💵 Cobrar / actualizar", "📁 Historial"])

    with sub_nuevo:
        if "pedido_recien" in st.session_state:
            with st.container(border=True):
                st.markdown(f"**📤 Avisa al equipo del pedido {folio(st.session_state.pedido_recien)}**")
                panel_compartir(st.session_state.pedido_recien, "recien")
                if st.button("Listo, ocultar", key="ocultar_recien"):
                    del st.session_state["pedido_recien"]
                    st.rerun()
        prods = cargar_productos()
        mapa = {r["nombre"]: r for r in prods.to_dict("records")}
        version = st.session_state.get("pedido_version", 0)
        c1, c2 = st.columns(2)
        cliente = c1.text_input("Cliente *", key=f"np_cliente_{version}",
                                help="Nombre de quien hace el pedido. Es obligatorio.")
        telefono = c2.text_input("Teléfono / WhatsApp", key=f"np_tel_{version}", placeholder="10 dígitos",
                                 help="Opcional. Con él aparecen los botones para avisarle por WhatsApp "
                                      "cuando esté listo o recordarle el saldo.")
        c1, c2 = st.columns(2)
        fecha_ped = c1.date_input("Fecha del pedido", value=hoy(), key=f"np_fecha_{version}", format="DD/MM/YYYY",
                                  help="Día en que el cliente hizo el pedido. El anticipo se anota en esta fecha.")
        fecha_ent = c2.date_input("Fecha de entrega", value=hoy() + timedelta(days=15),
                                  key=f"np_entrega_{version}", format="DD/MM/YYYY",
                                  help="Día prometido al cliente. La app avisa cuando se acerca o ya pasó.")

        st.caption("Elige piezas del catálogo o «Pieza personalizada» y descríbela (diseño, colores, medida).")
        capturado = st.data_editor(
            tabla_vacia(con_descripcion=True), key=f"np_editor_{version}", num_rows="dynamic",
            hide_index=True, use_container_width=True,
            column_config={
                "Pieza": st.column_config.SelectboxColumn(
                    "Pieza", options=[PERSONALIZADA] + list(mapa.keys()), width="medium",
                    help="Elige una pieza del catálogo, o «Pieza personalizada» si se hará especial."),
                "Descripción": st.column_config.TextColumn(
                    "Descripción", width="large",
                    help="Obligatoria en piezas personalizadas: diseño, colores, medida, nombre que lleva..."),
                "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=1, step=1, default=1,
                                                          help="Cuántas piezas de este tipo."),
                "Precio c/u": st.column_config.NumberColumn(
                    "Precio c/u", min_value=0.0, step=1.0, format="$%.2f",
                    help="En piezas del catálogo, vacío = precio de lista. En personalizadas escribe el precio."),
            },
        )
        lineas, avisos = armar_lineas(capturado, mapa)
        for a in avisos:
            st.warning(a)
        total_p = sum(l["subtotal"] for l in lineas)
        if lineas:
            resumen_lineas(lineas)
            st.markdown(f"### Total del pedido: {dinero(total_p)}")

        c1, c2 = st.columns(2)
        anticipo = c1.number_input("Anticipo recibido", min_value=0.0, step=50.0, format="%.2f",
                                   key=f"np_anticipo_{version}",
                                   help="Lo que el cliente dejó a cuenta. Déjalo en 0 si no dejó nada; "
                                        "los pagos siguientes se anotan en «Cobrar / actualizar».")
        metodo_a = c2.selectbox("Forma de pago del anticipo", METODOS_PAGO, key=f"np_metodo_{version}",
                                help="Cómo pagó el anticipo.")
        if lineas:
            st.caption(f"Saldo pendiente: **{dinero(total_p - anticipo)}**")
        notas_p = st.text_area("Notas del pedido", key=f"np_notas_{version}",
                               placeholder="Ej. lleva su nombre al centro, colores azul y amarillo",
                               help="Detalles para el taller. Aparecen en el mensaje que se comparte con el equipo.")

        col_ok, col_cancel = st.columns([2, 1])
        if col_cancel.button("❌ Cancelar pedido", use_container_width=True, key=f"np_cancelar_{version}",
                             help="Borra lo que capturaste sin guardar nada (por ejemplo, si el cliente "
                                  "ya no quiso hacer el pedido)."):
            st.session_state.pedido_version = version + 1
            avisar("Pedido cancelado, no se guardó nada.", "info")
            st.rerun()
        if col_ok.button("💾 Guardar pedido", type="primary", use_container_width=True,
                         help="Guarda el pedido y, si hay anticipo, lo anota en caja."):
            if not cliente.strip():
                st.error("Escribe el nombre del cliente.")
            elif not lineas:
                st.error("Agrega al menos una pieza al pedido.")
            elif anticipo > total_p:
                st.error("El anticipo no puede ser mayor que el total.")
            else:
                pid = crear_pedido(cliente.strip(), telefono.strip(), fecha_ped, fecha_ent,
                                   notas_p.strip(), lineas, anticipo, metodo_a)
                st.session_state.pedido_version = version + 1
                st.session_state.pedido_recien = pid
                avisar(f"✅ Pedido {folio(pid)} de {cliente.strip()} guardado por {dinero(total_p)}.")
                st.rerun()

    with sub_activos:
        activos = cargar_pedidos(" WHERE p.estado NOT IN ('Entregado', 'Cancelado')")
        if activos.empty:
            st.info("No hay pedidos activos.")
        else:
            h = hoy()

            def cuando(f):
                if f is None or pd.isna(f):
                    return "Sin fecha"
                dias = (f - h).days
                if dias < 0:
                    return f"⏰ Atrasado {-dias} día(s)"
                if dias == 0:
                    return "📍 Hoy"
                return f"En {dias} día(s)"

            vista = pd.DataFrame({
                "Folio": activos["id"].map(folio), "Cliente": activos["cliente"],
                "Teléfono": activos["telefono"], "Entrega": activos["fecha_entrega"],
                "Cuándo": activos["fecha_entrega"].map(cuando), "Estado": activos["estado"],
                "Total": activos["total"], "Pagado": activos["pagado"], "Saldo": activos["saldo"],
            })
            st.dataframe(vista, hide_index=True, use_container_width=True,
                         column_config={"Total": DINERO_COL, "Pagado": DINERO_COL, "Saldo": DINERO_COL})
            st.caption(f"{len(activos)} pedido(s) · por cobrar {dinero(activos['saldo'].clip(lower=0).sum(), OCULTAR)}")

    with sub_cobrar:
        mostrar_todos = st.checkbox("Mostrar también pedidos entregados y pagados",
            help="Normalmente solo aparecen los pedidos que falta entregar o cobrar.")
        todos = cargar_pedidos(" WHERE p.estado <> 'Cancelado'")
        if not mostrar_todos and not todos.empty:
            todos = todos[(todos["estado"] != "Entregado") | (todos["saldo"] > 0.005)]
        if todos.empty:
            st.info("No hay pedidos pendientes.")
        else:
            etiquetas = {
                int(r["id"]): f"{folio(r['id'])} · {r['cliente']} · {r['estado']} · saldo {dinero(r['saldo'])}"
                for _, r in todos.iterrows()}
            pid = st.selectbox("Pedido", list(etiquetas.keys()), format_func=etiquetas.get,
                help="Elige el pedido para ver sus piezas, cobrar, cambiar su estado o modificarlo.")
            p = todos[todos["id"] == pid].iloc[0]

            st.markdown(f"### Pedido {folio(pid)} · {p['cliente']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total", dinero(p["total"]))
            c2.metric("Pagado", dinero(p["pagado"]))
            c3.metric("Saldo", dinero(p["saldo"]))
            c4.metric("Estado", p["estado"])
            entrega_txt = p["fecha_entrega"].strftime("%d/%m/%Y") if p["fecha_entrega"] is not None and not pd.isna(p["fecha_entrega"]) else "sin fecha"
            st.caption(f"📞 {p['telefono'] or 'sin teléfono'} · pedido el {p['fecha_pedido'].strftime('%d/%m/%Y')}"
                       f" · entrega {entrega_txt}" + (f" · 📝 {p['notas']}" if p["notas"] else ""))

            detalle = consultar_df(
                'SELECT descripcion AS "Pieza", cantidad AS "Cantidad", precio_unitario AS "Precio c/u",'
                ' cantidad * precio_unitario AS "Subtotal" FROM pedido_detalle WHERE pedido_id = %s ORDER BY id',
                (pid,))
            st.dataframe(detalle, hide_index=True, use_container_width=True,
                         column_config={"Precio c/u": DINERO_COL, "Subtotal": DINERO_COL})
            pagos = consultar_df(
                'SELECT fecha AS "Fecha", categoria AS "Concepto", metodo AS "Forma de pago", monto AS "Monto"'
                " FROM caja WHERE pedido_id = %s AND tipo = 'INGRESO' ORDER BY fecha, id", (pid,))
            if not pagos.empty:
                st.markdown("**Pagos recibidos**")
                st.dataframe(pagos, hide_index=True, use_container_width=True, column_config={"Monto": DINERO_COL})

            izq, der = st.columns(2)
            with izq:
                with st.form(f"abono_{pid}"):
                    st.markdown("**💵 Registrar pago**")
                    monto = st.number_input("Monto", min_value=0.0, step=50.0, format="%.2f",
                                            value=float(max(p["saldo"], 0)),
                                            help="Ya viene con el saldo completo. Cámbialo si el cliente "
                                                 "solo dejó una parte.")
                    metodo_ab = st.selectbox("Forma de pago", METODOS_PAGO,
                        help="Cómo pagó el cliente este abono.")
                    fecha_ab = st.date_input("Fecha", value=hoy(), format="DD/MM/YYYY",
                        help="Día en que se recibió el pago.")
                    if st.form_submit_button("Registrar pago", use_container_width=True):
                        if monto <= 0:
                            st.error("Escribe un monto mayor a cero.")
                        else:
                            registrar_abono(pid, p["cliente"], monto, metodo_ab, fecha_ab)
                            restante = p["saldo"] - monto
                            avisar(f"✅ Pago de {dinero(monto)} registrado. "
                                   + ("¡Pedido liquidado! 🎉" if restante <= 0.005 else f"Resta {dinero(restante)}."))
                            st.rerun()
            with der:
                lleva_catalogo = valor(
                    "SELECT COUNT(*) FROM pedido_detalle WHERE pedido_id = %s AND producto_id IS NOT NULL", (pid,))
                with st.form(f"estado_{pid}"):
                    st.markdown("**🔄 Cambiar estado**")
                    nuevo_estado = st.selectbox("Estado", ESTADOS_PEDIDO, index=ESTADOS_PEDIDO.index(p["estado"])
                                                if p["estado"] in ESTADOS_PEDIDO else 0,
                                                help="En qué va el pedido. Si el cliente ya no lo quiere, "
                                                     "elige «Cancelado»: deja de contar como pendiente y "
                                                     "por cobrar, pero queda en el historial.")
                    descontar = False
                    if lleva_catalogo and not p["inventario_descontado"]:
                        descontar = st.checkbox(
                            "Al entregar, descontar del inventario las piezas de catálogo", value=True,
                            help="Desmárcalo si las piezas se hicieron especialmente para este pedido.")
                    if st.form_submit_button("Guardar estado", use_container_width=True):
                        try:
                            cambiar_estado_pedido(pid, nuevo_estado, descontar, hoy())
                            msg = f"✅ Pedido {folio(pid)} ahora está «{nuevo_estado}»."
                            if nuevo_estado == "Cancelado" and p["pagado"] > 0:
                                msg += (" Si devolviste el anticipo, regístralo en 💰 Cuentas como gasto"
                                        " «Devolución a cliente».")
                            avisar(msg)
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            listo = link_whatsapp(p["telefono"], f"Hola {p['cliente']}, le escribimos de {NEGOCIO}. "
                                  f"Su pedido {folio(pid)} ya está listo para entregar."
                                  + (f" Saldo pendiente: {dinero(p['saldo'])}." if p["saldo"] > 0.005 else ""))
            if listo:
                c1, c2 = st.columns(2)
                c1.link_button("📲 Avisar por WhatsApp que está listo", listo, use_container_width=True)
                if p["saldo"] > 0.005:
                    recordar = link_whatsapp(p["telefono"], f"Hola {p['cliente']}, le escribimos de {NEGOCIO}. "
                                             f"Le recordamos que su pedido {folio(pid)} tiene un saldo de "
                                             f"{dinero(p['saldo'])}. ¡Gracias!")
                    c2.link_button("📲 Recordar saldo por WhatsApp", recordar, use_container_width=True)

            with st.expander("✏️ Modificar pedido"):
                ver_ed = st.session_state.get(f"edit_ver_{pid}", 0)
                prods_ed = cargar_productos()
                mapa_ed = {r["nombre"]: r for r in prods_ed.to_dict("records")}
                id_a_nombre = {int(r["id"]): r["nombre"] for r in prods_ed.to_dict("records")}
                c1, c2 = st.columns(2)
                ed_cliente = c1.text_input("Cliente", value=p["cliente"], key=f"ed_cli_{pid}_{ver_ed}",
                                           help="Corrige el nombre si se escribió mal.")
                ed_tel = c2.text_input("Teléfono / WhatsApp", value=p["telefono"], key=f"ed_tel_{pid}_{ver_ed}",
                                       help="10 dígitos. Se usa para los botones de WhatsApp al cliente.")
                tiene_fecha = p["fecha_entrega"] is not None and not pd.isna(p["fecha_entrega"])
                ed_entrega = st.date_input("Fecha de entrega", value=p["fecha_entrega"] if tiene_fecha else None,
                                           key=f"ed_ent_{pid}_{ver_ed}", format="DD/MM/YYYY",
                                           help="Cámbiala si se acordó otra fecha con el cliente.")
                ed_notas = st.text_area("Notas", value=p["notas"], key=f"ed_notas_{pid}_{ver_ed}",
                                        help="Detalles para el taller.")

                lineas_ed = None
                if p["inventario_descontado"]:
                    st.info("Las piezas ya se descontaron del inventario al entregar, así que solo se pueden "
                            "cambiar los datos de arriba.")
                else:
                    filas = consultar("SELECT producto_id, descripcion, cantidad, precio_unitario FROM pedido_detalle"
                                      " WHERE pedido_id = %s ORDER BY id", (pid,))
                    actual = pd.DataFrame({
                        "Pieza": pd.Series([id_a_nombre.get(int(f[0]), PERSONALIZADA) if f[0] is not None
                                            else PERSONALIZADA for f in filas], dtype="object"),
                        "Descripción": pd.Series([f[1] for f in filas], dtype="object"),
                        "Cantidad": pd.Series([int(f[2]) for f in filas], dtype="int64"),
                        "Precio c/u": pd.Series([float(f[3]) for f in filas], dtype="float64"),
                    })
                    st.caption("Piezas del pedido: puedes cambiar cantidades y precios, agregar filas o borrar "
                               "las que sobren.")
                    capturado_ed = st.data_editor(
                        actual, key=f"ed_piezas_{pid}_{ver_ed}", num_rows="dynamic", hide_index=True,
                        use_container_width=True,
                        column_config={
                            "Pieza": st.column_config.SelectboxColumn(
                                "Pieza", options=[PERSONALIZADA] + list(mapa_ed.keys()), width="medium"),
                            "Descripción": st.column_config.TextColumn("Descripción", width="large"),
                            "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=1, step=1, default=1),
                            "Precio c/u": st.column_config.NumberColumn("Precio c/u", min_value=0.0, step=1.0,
                                                                        format="$%.2f"),
                        },
                    )
                    lineas_ed, avisos_ed = armar_lineas(capturado_ed, mapa_ed)
                    for a in avisos_ed:
                        st.warning(a)
                    nuevo_total = sum(l["subtotal"] for l in lineas_ed)
                    st.markdown(f"**Nuevo total: {dinero(nuevo_total)}** (antes {dinero(p['total'])}) · "
                                f"nuevo saldo: {dinero(nuevo_total - p['pagado'])}")
                    if nuevo_total < p["pagado"] - 0.005:
                        st.warning(f"El cliente ya pagó {dinero(p['pagado'])}, más que el nuevo total. "
                                   "Si le devuelves la diferencia, regístrala en 💰 Cuentas como «Devolución a cliente».")

                col_ok, col_cancel = st.columns([2, 1])
                if col_cancel.button("↩️ Descartar cambios", key=f"ed_descartar_{pid}_{ver_ed}",
                                     use_container_width=True,
                                     help="Deja el pedido como estaba, sin guardar lo que cambiaste aquí."):
                    st.session_state[f"edit_ver_{pid}"] = ver_ed + 1
                    avisar(f"Cambios descartados. El pedido {folio(pid)} quedó como estaba.", "info")
                    st.rerun()
                if col_ok.button("💾 Guardar cambios del pedido", key=f"ed_guardar_{pid}_{ver_ed}",
                                 use_container_width=True, type="primary",
                                 help="Guarda los cambios. El total y el saldo se recalculan solos."):
                    if not ed_cliente.strip():
                        st.error("El pedido necesita el nombre del cliente.")
                    elif lineas_ed is not None and not lineas_ed:
                        st.error("El pedido debe tener al menos una pieza. Si ya no va, cámbialo a «Cancelado».")
                    else:
                        try:
                            editar_pedido(pid, ed_cliente.strip(), ed_tel.strip(), ed_entrega,
                                          ed_notas.strip(), lineas_ed)
                            st.session_state[f"edit_ver_{pid}"] = ver_ed + 1
                            avisar(f"✅ Pedido {folio(pid)} actualizado. Si cambió algo importante, compártelo "
                                   "otra vez con el equipo.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            with st.expander("📤 Compartir este pedido con el equipo"):
                panel_compartir(pid, f"detalle_{pid}")

    with sub_historial:
        c1, c2 = st.columns([2, 1])
        buscar = c1.text_input("Buscar cliente", key="hist_buscar",
            help="Escribe parte del nombre del cliente.")
        estados = c2.multiselect("Estado", ESTADOS_PEDIDO, default=[], key="hist_estados",
            help="Déjalo vacío para ver todos los estados.")
        hist = cargar_pedidos()
        if not hist.empty:
            if buscar:
                hist = hist[hist["cliente"].str.contains(buscar, case=False, na=False)]
            if estados:
                hist = hist[hist["estado"].isin(estados)]
        if hist.empty:
            st.info("No hay pedidos que coincidan.")
        else:
            st.dataframe(pd.DataFrame({
                "Folio": hist["id"].map(folio), "Cliente": hist["cliente"], "Teléfono": hist["telefono"],
                "Pedido": hist["fecha_pedido"], "Entrega": hist["fecha_entrega"], "Estado": hist["estado"],
                "Total": hist["total"], "Pagado": hist["pagado"], "Saldo": hist["saldo"], "Notas": hist["notas"],
            }), hide_index=True, use_container_width=True,
                column_config={"Total": DINERO_COL, "Pagado": DINERO_COL, "Saldo": DINERO_COL})

# ------------------------------------------
# TAB: INVENTARIO
# ------------------------------------------
with tab_inventario:
    todos_prods = cargar_productos(solo_activos=False)
    activos_prods = todos_prods[todos_prods["activo"]] if not todos_prods.empty else todos_prods

    if not activos_prods.empty:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🏺 Piezas en tienda", int(activos_prods["existencias"].sum()))
        c2.metric("💲 Valor a costo", dinero((activos_prods["existencias"] * activos_prods["costo"]).sum(), OCULTAR))
        c3.metric("🏷️ Valor a precio de venta",
                  dinero((activos_prods["existencias"] * activos_prods["precio_venta"]).sum(), OCULTAR))
        c4.metric("⚠️ Por acabarse",
                  int((activos_prods["existencias"] <= activos_prods["stock_minimo"]).sum()))

    nombres_activos = {r["nombre"]: int(r["id"]) for _, r in activos_prods.iterrows()} if not activos_prods.empty else {}

    with st.expander("➕ Dar de alta una pieza nueva", expanded=todos_prods.empty):
        with st.form("nueva_pieza", clear_on_submit=True):
            c1, c2 = st.columns(2)
            n_nombre = c1.text_input("Nombre de la pieza *", placeholder="Ej. Plato pozolero flor azul",
                help="Nombre con el que la vas a buscar al vender. No puede repetirse.")
            n_cat = c2.selectbox("Categoría", CATEGORIAS,
                help="Sirve para agrupar y filtrar el catálogo.")
            c1, c2 = st.columns(2)
            n_medida = c1.text_input("Medida / tamaño", placeholder="Ej. 25 cm, mediano",
                help="Opcional.")
            n_diseno = c2.text_input("Diseño / colores", placeholder="Ej. azul cobalto, colonial",
                help="Opcional. También se puede buscar por diseño.")
            c1, c2 = st.columns(2)
            n_exist = c1.number_input("Piezas que hay ahorita", min_value=0, step=1,
                help="Cuántas hay terminadas y listas para vender en este momento.")
            n_min = c2.number_input("Avisarme cuando queden", min_value=0, value=2, step=1,
                help="Cuando queden estas piezas o menos, aparece el aviso de «por acabarse».")
            c1, c2, c3 = st.columns(3)
            n_costo = c1.number_input("Costo por pieza", min_value=0.0, step=5.0, format="%.2f",
                                      help="Lo que cuesta hacerla o comprarla.")
            n_precio = c2.number_input("Precio de venta", min_value=0.0, step=5.0, format="%.2f",
                help="Precio normal al público. Se usa al vender si no escribes otro.")
            n_mayoreo = c3.number_input("Precio de mayoreo (opcional)", min_value=0.0, step=5.0, format="%.2f",
                help="Déjalo en 0 si no manejas mayoreo para esta pieza.")
            if st.form_submit_button("💾 Guardar pieza", use_container_width=True):
                if not n_nombre.strip():
                    st.error("La pieza necesita un nombre.")
                else:
                    try:
                        crear_producto(n_nombre.strip(), n_cat, n_medida.strip(), n_diseno.strip(), int(n_exist),
                                       int(n_min), n_costo, n_precio, n_mayoreo, hoy())
                        avisar(f"✨ «{n_nombre.strip()}» agregada al catálogo.")
                        st.rerun()
                    except psycopg2.errors.UniqueViolation:
                        st.error("Ya existe una pieza con ese nombre. Usa un nombre distinto o edita la existente.")

    if nombres_activos:
        with st.expander("📥 Entraron piezas (salieron del horno o se compraron)"):
            with st.form("entrada_piezas", clear_on_submit=True):
                e_pieza = st.selectbox("Pieza", list(nombres_activos.keys()),
                    help="La pieza que entró. Si es nueva, primero dala de alta.")
                c1, c2, c3 = st.columns(3)
                e_cant = c1.number_input("Cantidad", min_value=1, step=1,
                    help="Cuántas piezas buenas entraron (sin contar las que salieron quebradas).")
                e_costo = c2.number_input("Costo por pieza (opcional)", min_value=0.0, step=5.0, format="%.2f",
                                          help="Si lo dejas en 0 se queda el costo que ya tenía.")
                e_fecha = c3.date_input("Fecha", value=hoy(), format="DD/MM/YYYY",
                    help="Día en que entraron las piezas.")
                e_nota = st.text_input("Nota", placeholder="Ej. hornada del martes",
                    help="Opcional.")
                if st.form_submit_button("📥 Sumar al inventario", use_container_width=True):
                    entrada_producto(nombres_activos[e_pieza], int(e_cant), e_costo, e_fecha, e_nota.strip())
                    avisar(f"📥 Se sumaron {int(e_cant)} «{e_pieza}».")
                    st.rerun()

        with st.expander("💥 Piezas quebradas o corregir conteo"):
            with st.form("merma_ajuste", clear_on_submit=True):
                m_pieza = st.selectbox("Pieza", list(nombres_activos.keys()),
                    help="La pieza que se quebró o que contaste.")
                m_tipo = st.radio("¿Qué pasó?", ["💥 Se quebraron / dañaron", "🔢 Conté y hay otra cantidad"],
                                  horizontal=True,
                                  help="Quebradas: se restan del inventario. Conteo: el inventario queda "
                                       "con el número que escribas.")
                c1, c2 = st.columns(2)
                m_cant = c1.number_input("Cantidad (quebradas, o las que hay realmente)", min_value=0, step=1,
                    help="Si se quebraron: cuántas. Si contaste: el total que hay en realidad.")
                m_fecha = c2.date_input("Fecha", value=hoy(), format="DD/MM/YYYY",
                    help="Día en que pasó.")
                m_nota = st.text_input("Nota", placeholder="Ej. se cayó el anaquel",
                    help="Opcional, para recordar qué pasó.")
                if st.form_submit_button("Guardar", use_container_width=True):
                    try:
                        if m_tipo.startswith("💥"):
                            if m_cant <= 0:
                                raise ValueError("Escribe cuántas piezas se quebraron.")
                            merma_producto(nombres_activos[m_pieza], int(m_cant), m_fecha, m_nota.strip())
                            avisar(f"💥 Se descontaron {int(m_cant)} «{m_pieza}» por merma.")
                        else:
                            corregir_conteo(nombres_activos[m_pieza], int(m_cant), m_fecha, m_nota.strip())
                            avisar(f"🔢 «{m_pieza}» corregida a {int(m_cant)} piezas.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    if not todos_prods.empty:
        with st.expander("✏️ Editar o eliminar una pieza"):
            ids = {int(r["id"]): r["nombre"] + ("" if r["activo"] else " (inactiva)")
                   for _, r in todos_prods.iterrows()}
            ed_id = st.selectbox("Pieza a editar", list(ids.keys()), format_func=ids.get, key="editar_sel",
                help="Elige la pieza que quieres cambiar.")
            r = todos_prods[todos_prods["id"] == ed_id].iloc[0]
            with st.form(f"editar_{ed_id}"):
                c1, c2 = st.columns(2)
                ed_nombre = c1.text_input("Nombre", value=r["nombre"],
                    help="No puede repetirse con otra pieza.")
                ed_cat = c2.selectbox("Categoría", CATEGORIAS,
                                      index=CATEGORIAS.index(r["categoria"]) if r["categoria"] in CATEGORIAS else 0,
                                      help="Sirve para agrupar y filtrar el catálogo.")
                c1, c2, c3 = st.columns(3)
                ed_medida = c1.text_input("Medida", value=r["medida"],
                    help="Opcional.")
                ed_diseno = c2.text_input("Diseño / colores", value=r["diseno"],
                    help="Opcional.")
                ed_min = c3.number_input("Avisarme cuando queden", min_value=0, value=int(r["stock_minimo"]), step=1,
                    help="Cuando queden estas piezas o menos, aparece el aviso de «por acabarse».")
                c1, c2, c3 = st.columns(3)
                ed_costo = c1.number_input("Costo", min_value=0.0, value=float(r["costo"]), step=5.0, format="%.2f",
                    help="Lo que cuesta hacer o comprar una pieza. Sirve para calcular la ganancia.")
                ed_precio = c2.number_input("Precio de venta", min_value=0.0, value=float(r["precio_venta"]),
                                            step=5.0, format="%.2f",
                                            help="Cambiarlo no afecta ventas ni pedidos ya registrados.")
                ed_mayoreo = c3.number_input("Mayoreo", min_value=0.0, value=float(r["precio_mayoreo"]),
                                             step=5.0, format="%.2f",
                                             help="0 = sin precio de mayoreo (se usa el normal).")
                ed_activo = st.checkbox("Activa (aparece para vender y en pedidos)", value=bool(r["activo"]),
                    help="Desmárcala si ya no se hace esta pieza: deja de aparecer pero conserva su historial.")
                st.caption(f"Existencias actuales: {int(r['existencias'])}. Para cambiarlas usa "
                           "«Entraron piezas» o «Corregir conteo».")
                confirmar = st.checkbox("Sí, quiero eliminar esta pieza del catálogo",
                    help="Eliminar no se puede deshacer. Si solo ya no se vende, mejor desmarca «Activa».")
                c1, c2 = st.columns(2)
                guardar = c1.form_submit_button("💾 Guardar cambios", use_container_width=True)
                borrar = c2.form_submit_button("🗑️ Eliminar", use_container_width=True)
                if guardar:
                    try:
                        with transaccion() as cur:
                            cur.execute(
                                "UPDATE productos SET nombre = %s, categoria = %s, medida = %s, diseno = %s,"
                                " stock_minimo = %s, costo = %s, precio_venta = %s, precio_mayoreo = %s,"
                                " activo = %s WHERE id = %s",
                                (ed_nombre.strip(), ed_cat, ed_medida.strip(), ed_diseno.strip(), int(ed_min),
                                 ed_costo, ed_precio, ed_mayoreo, ed_activo, ed_id))
                        avisar("✅ Pieza actualizada.")
                        st.rerun()
                    except psycopg2.errors.UniqueViolation:
                        st.error("Ya existe otra pieza con ese nombre.")
                if borrar:
                    if not confirmar:
                        st.warning("Marca la casilla de confirmación para eliminar. "
                                   "Tip: si ya no la haces, mejor desmárcala como «Activa» y conservas su historial.")
                    else:
                        with transaccion() as cur:
                            cur.execute("DELETE FROM productos WHERE id = %s", (ed_id,))
                        avisar("🗑️ Pieza eliminada.")
                        st.rerun()

    st.markdown("#### 📋 Catálogo e inventario")
    if todos_prods.empty:
        st.info("Todavía no hay piezas. Empieza dando de alta la primera arriba. 👆")
    else:
        c1, c2, c3 = st.columns([2, 2, 1])
        f_buscar = c1.text_input("Buscar", key="inv_buscar", placeholder="Nombre o diseño",
            help="Escribe parte del nombre o del diseño.")
        f_cat = c2.multiselect("Categoría", CATEGORIAS, key="inv_cat",
            help="Déjalo vacío para ver todas.")
        f_bajo = c3.checkbox("Solo por acabarse", key="inv_bajo",
            help="Muestra solo las piezas en su mínimo o por debajo.")
        f_inactivas = c3.checkbox("Ver inactivas", key="inv_inactivas",
            help="Incluye las piezas que ya no se venden.")
        vista = todos_prods if f_inactivas else activos_prods
        if f_buscar:
            texto = vista["nombre"] + " " + vista["diseno"]
            vista = vista[texto.str.contains(f_buscar, case=False, na=False)]
        if f_cat:
            vista = vista[vista["categoria"].isin(f_cat)]
        if f_bajo:
            vista = vista[vista["existencias"] <= vista["stock_minimo"]]
        st.dataframe(pd.DataFrame({
            "Pieza": vista["nombre"], "Categoría": vista["categoria"], "Medida": vista["medida"],
            "Diseño": vista["diseno"], "Hay": vista["existencias"],
            "Aviso": vista.apply(lambda x: "⚠️ Por acabarse" if x["existencias"] <= x["stock_minimo"] else "", axis=1)
            if not vista.empty else [],
            "Costo": vista["costo"], "Precio": vista["precio_venta"], "Mayoreo": vista["precio_mayoreo"],
        }), hide_index=True, use_container_width=True,
            column_config={"Costo": DINERO_COL, "Precio": DINERO_COL, "Mayoreo": DINERO_COL})

        with st.expander("📅 Bitácora de movimientos de piezas"):
            bit = consultar_df(
                'SELECT fecha AS "Fecha", tipo AS "Tipo", nombre_producto AS "Pieza", cantidad AS "Cantidad",'
                ' precio_unitario AS "Precio", nota AS "Nota" FROM movimientos_inventario'
                " ORDER BY fecha DESC, id DESC LIMIT 200")
            if bit.empty:
                st.info("Sin movimientos todavía.")
            else:
                st.dataframe(bit, hide_index=True, use_container_width=True, column_config={"Precio": DINERO_COL})

# ------------------------------------------
# TAB: CUENTAS
# ------------------------------------------
with tab_cuentas:
    sub_registrar, sub_corte, sub_mes = st.tabs(["➕ Gasto / ingreso", "🧾 Corte del día", "📆 Resumen del mes"])

    with sub_registrar:
        st.caption("Las ventas y los pagos de pedidos se registran solos. Aquí anota los gastos "
                   "y cualquier otro dinero que entre.")
        izq, der = st.columns(2)
        with izq:
            with st.form("gasto", clear_on_submit=True):
                st.markdown("**📉 Registrar gasto**")
                g_cat = st.selectbox("Tipo de gasto", CATEGORIAS_GASTO,
                    help="Sirve para ver en qué se va el dinero en el resumen del mes.")
                g_concepto = st.text_input("Concepto", placeholder="Ej. 2 costales de barro",
                    help="Qué se compró o pagó, para recordarlo después.")
                g_monto = st.number_input("Monto", min_value=0.0, step=10.0, format="%.2f",
                    help="Cuánto se pagó en total.")
                c1, c2 = st.columns(2)
                g_metodo = c1.selectbox("Forma de pago", METODOS_PAGO,
                    help="Si se pagó en efectivo, se resta del efectivo del corte.")
                g_fecha = c2.date_input("Fecha", value=hoy(), format="DD/MM/YYYY",
                    help="Día en que se pagó.")
                if st.form_submit_button("Guardar gasto", use_container_width=True):
                    if g_monto <= 0:
                        st.error("Escribe un monto mayor a cero.")
                    else:
                        registrar_caja(g_fecha, "GASTO", g_cat, g_concepto.strip(), g_monto, g_metodo)
                        avisar(f"📉 Gasto de {dinero(g_monto)} registrado.")
                        st.rerun()
        with der:
            with st.form("ingreso", clear_on_submit=True):
                st.markdown("**📈 Registrar otro ingreso**")
                i_cat = st.selectbox("Tipo", CATEGORIAS_INGRESO_MANUAL,
                                     help="«Venta sin inventario» es para piezas que no están en el catálogo.")
                i_concepto = st.text_input("Concepto", placeholder="Ej. venta en feria de San Pedro",
                    help="De dónde vino el dinero.")
                i_monto = st.number_input("Monto", min_value=0.0, step=10.0, format="%.2f",
                    help="Cuánto entró.")
                c1, c2 = st.columns(2)
                i_metodo = c1.selectbox("Forma de pago", METODOS_PAGO,
                    help="Cómo se recibió el dinero.")
                i_fecha = c2.date_input("Fecha", value=hoy(), format="DD/MM/YYYY",
                    help="Día en que entró.")
                if st.form_submit_button("Guardar ingreso", use_container_width=True):
                    if i_monto <= 0:
                        st.error("Escribe un monto mayor a cero.")
                    else:
                        registrar_caja(i_fecha, "INGRESO", i_cat, i_concepto.strip(), i_monto, i_metodo)
                        avisar(f"📈 Ingreso de {dinero(i_monto)} registrado.")
                        st.rerun()

    with sub_corte:
        dia = st.date_input("Día", value=hoy(), key="corte_dia", format="DD/MM/YYYY",
            help="Elige el día del que quieres ver el corte.")
        movs = consultar_df(
            "SELECT id, registrado, tipo, categoria, concepto, metodo, monto, pedido_id"
            " FROM caja WHERE fecha = %s ORDER BY registrado, id", (dia,))
        ingresos_d = movs.loc[movs["tipo"] == "INGRESO", "monto"].sum() if not movs.empty else 0
        gastos_d = movs.loc[movs["tipo"] == "GASTO", "monto"].sum() if not movs.empty else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("📈 Entró", dinero(ingresos_d, OCULTAR))
        c2.metric("📉 Salió", dinero(gastos_d, OCULTAR))
        c3.metric("⚖️ Balance del día", dinero(ingresos_d - gastos_d, OCULTAR))

        if movs.empty:
            st.info("No hay movimientos de dinero en este día.")
        else:
            por_metodo = movs.assign(
                Entró=movs["monto"].where(movs["tipo"] == "INGRESO", 0),
                Salió=movs["monto"].where(movs["tipo"] == "GASTO", 0),
            ).groupby("metodo", as_index=False)[["Entró", "Salió"]].sum()
            por_metodo["Neto"] = por_metodo["Entró"] - por_metodo["Salió"]
            efectivo = por_metodo.loc[por_metodo["metodo"] == "Efectivo", "Neto"].sum()
            st.markdown(f"**💵 Efectivo neto del día (lo que debe sumar a la caja): {dinero(efectivo, OCULTAR)}**")
            if not OCULTAR:
                st.dataframe(por_metodo.rename(columns={"metodo": "Forma de pago"}), hide_index=True,
                             use_container_width=True,
                             column_config={"Entró": DINERO_COL, "Salió": DINERO_COL, "Neto": DINERO_COL})
                st.dataframe(pd.DataFrame({
                    "Hora": pd.to_datetime(movs["registrado"]).dt.strftime("%H:%M"),
                    "Tipo": movs["tipo"].map({"INGRESO": "📈 Ingreso", "GASTO": "📉 Gasto"}),
                    "Categoría": movs["categoria"], "Concepto": movs["concepto"],
                    "Forma de pago": movs["metodo"], "Monto": movs["monto"],
                }), hide_index=True, use_container_width=True, column_config={"Monto": DINERO_COL})

            with st.expander("🗑️ Borrar un movimiento equivocado"):
                opciones = {int(r["id"]): f"{r['categoria']} · {r['concepto'][:50]} · {dinero(r['monto'])}"
                            for _, r in movs.iterrows()}
                borrar_id = st.selectbox("Movimiento", list(opciones.keys()), format_func=opciones.get,
                                         key="borrar_caja_sel",
                                         help="Solo aparecen los movimientos del día elegido arriba.")
                fila = movs[movs["id"] == borrar_id].iloc[0]
                if fila["categoria"] == "Venta mostrador":
                    st.caption("Es una venta: al borrarla, las piezas regresan al inventario.")
                elif fila["pedido_id"] is not None and not pd.isna(fila["pedido_id"]):
                    st.caption(f"Es un pago del pedido {folio(fila['pedido_id'])}: su saldo volverá a subir.")
                seguro = st.checkbox("Sí, quiero borrarlo", key="borrar_caja_ok",
                    help="Borrar no se puede deshacer.")
                if st.button("Borrar movimiento", disabled=not seguro):
                    eliminar_caja(borrar_id)
                    avisar("🗑️ Movimiento borrado.")
                    st.rerun()

    with sub_mes:
        meses = meses_recientes(12)
        mes = st.selectbox("Mes", meses, format_func=nombre_mes, key="resumen_mes",
            help="Elige el mes que quieres revisar.")
        fin = siguiente_mes(mes)
        por_cat = consultar_df(
            "SELECT tipo, categoria, SUM(monto) AS monto FROM caja WHERE fecha >= %s AND fecha < %s"
            " GROUP BY tipo, categoria ORDER BY monto DESC", (mes, fin))
        ing = por_cat.loc[por_cat["tipo"] == "INGRESO", "monto"].sum() if not por_cat.empty else 0
        gas = por_cat.loc[por_cat["tipo"] == "GASTO", "monto"].sum() if not por_cat.empty else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("📈 Ingresos", dinero(ing, OCULTAR))
        c2.metric("📉 Gastos", dinero(gas, OCULTAR))
        c3.metric("⚖️ Balance", dinero(ing - gas, OCULTAR))

        if por_cat.empty:
            st.info("No hay movimientos en este mes.")
        elif not OCULTAR:
            izq, der = st.columns(2)
            for col, tipo, titulo, color in [(izq, "INGRESO", "¿De dónde entró el dinero?", PRIMARY_COLOR),
                                             (der, "GASTO", "¿En qué se gastó?", ACENTO)]:
                with col:
                    st.markdown(f"**{titulo}**")
                    datos = por_cat[por_cat["tipo"] == tipo]
                    if datos.empty:
                        st.caption("Sin registros.")
                        continue
                    fig = px.bar(datos, x="monto", y="categoria", orientation="h",
                                 text=datos["monto"].map(lambda v: f"${v:,.0f}"),
                                 color_discrete_sequence=[color])
                    fig.update_layout(height=60 + 40 * len(datos), margin=dict(t=10, b=10, l=10, r=10),
                                      xaxis_title=None, yaxis_title=None, yaxis=dict(autorange="reversed"),
                                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
                    st.plotly_chart(fig, use_container_width=True)

            st.markdown("**Últimos 6 meses**")
            inicio_6 = meses_recientes(6)[-1]
            tendencia = consultar_df(
                "SELECT date_trunc('month', fecha)::date AS mes, tipo, SUM(monto) AS monto FROM caja"
                " WHERE fecha >= %s GROUP BY 1, 2 ORDER BY 1", (inicio_6,))
            if not tendencia.empty:
                tendencia["Mes"] = tendencia["mes"].map(nombre_mes)
                tendencia["tipo"] = tendencia["tipo"].map({"INGRESO": "Ingresos", "GASTO": "Gastos"})
                fig = px.bar(tendencia, x="Mes", y="monto", color="tipo", barmode="group",
                             color_discrete_map={"Ingresos": PRIMARY_COLOR, "Gastos": ACENTO})
                fig.update_layout(height=280, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None,
                                  yaxis_title=None, legend_title=None,
                                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, use_container_width=True)
