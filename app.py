import os
import datetime
import uuid
import base64
import io
from io import BytesIO
import csv
from collections import defaultdict

from flask import Flask, render_template, request, redirect, url_for, session, g, Response, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# 🚨 CAMBIO CRÍTICO 1: Reemplazar MySQLdb con psycopg2
# import MySQLdb as mdb
# import MySQLdb.cursors
import psycopg2
import psycopg2.extras # Para usar DictCursor

import qrcode

# --- Configuración de Flask ---
app = Flask(__name__)

# =========================================================================
# *** CONFIGURACIÓN CRÍTICA DE POSTGRESQL SIN ORM ***
# ⚠️ ALERTA: DEBES REEMPLAZAR TODOS ESTOS MARCADORES DE POSICIÓN ⚠️
# =========================================================================
# 🚨 CAMBIO CRÍTICO 2: Adaptar la configuración a PostgreSQL
DB_CONFIG = {
    'host': 'ep-old-pond-af37t6sb-pooler.c-2.us-west-2.aws.neon.tech',
    'user': 'neondb_owner',
    'password': 'npg_lvsqT6A1XZgk',         # <-- ¡REEMPLAZA! Ejemplo: 'root'
    'database': 'neondb',
    'sslmode': 'require'
}
# =========================================================================

# Usa una clave de sesión fuerte, esencial para la seguridad:
app.secret_key = os.environ.get('SECRET_KEY', 'una_clave_secreta_final')


# --- Hook para inyectar encabezados de seguridad (CSP Fix) ---
@app.after_request
def add_security_headers(response):
    # Política de Seguridad de Contenido (CSP) para permitir scripts de CDN y localhost/cámara
    csp = (
        "default-src 'self';"
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com;"
        "style-src 'self' 'unsafe-inline';"
        "img-src 'self' data:;"
        # CORRECCIÓN DE CSP: Permitir conexión a localhost:5000 para el fetch del escáner
        "connect-src 'self' http://localhost:5000 https://localhost:5000;"
        "media-src 'self' blob:;"
    )
    response.headers['Content-Security-Policy'] = csp
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


# --- Funciones de Conexión a la Base de Datos ---

def get_db():
    """Establece la conexión a PostgreSQL y la almacena en el objeto 'g' de Flask."""
    if 'db' not in g:
        try:
            # 🚨 CAMBIO 3: Conexión con psycopg2
            g.db = psycopg2.connect(**DB_CONFIG)
            g.db.autocommit = False
        except Exception as e:
            print(f"Error al conectar con PostgreSQL: {e}")
            raise e
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    """Cierra la conexión a PostgreSQL al finalizar la solicitud."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- Función para Generar QR ---

def generate_qr_code(data):
    """Genera un código QR y lo devuelve como imagen base64."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convertir la imagen a un stream de bytes y luego a base64
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{img_str}"

# --- Función Helper: Genera el HTML de la respuesta (BYPASS TemplateNotFound) ---
def build_checkin_response(message, status, full_name=None):
    """Genera el HTML de la página de resultado de check-in directamente en Python."""
    
    # Mapeo de estado a título y clase CSS
    status_map = {
        'success': ('¡Registro Exitoso! ✅', 'success'),
        'warning': ('Asistencia Registrada Hoy ⚠️', 'warning'),
        'error': ('Error al Registrar ❌', 'error')
    }
    title, css_class = status_map.get(status, ('Error Desconocido', 'error'))

    # Define el HTML para mostrar el nombre
    user_name_html = ""
    if full_name:
        user_name_html = f'<span class="user-name">{full_name}</span>'

    # Define el CSS para estilizar el nombre
    name_style = """
        .user-name {
            font-weight: bold;
            font-size: 1.2em;
            display: block;
            margin-bottom: 15px;
            color: #2c3e50;
        }
    """

    html_content = f"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Resultado de Asistencia</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        {name_style}
        body {{
            font-family: 'Inter', Arial, sans-serif;
            background-color: #f4f4f4;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            text-align: center;
        }}
        .container {{
            background-color: #fff;
            padding: 40px;
            border-radius: 10px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
            max-width: 400px;
            width: 90%;
        }}
        .message-box {{
            padding: 25px;
            border-radius: 8px;
            margin-bottom: 20px;
        }}
        .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
        .warning {{ background-color: #fffff0; color: #856404; border: 1px solid #ffeeba; }}
        .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        
        h2 {{
            margin-top: 0;
            font-size: 1.5em;
        }}
        p {{
            font-size: 1.1em;
            line-height: 1.4;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="message-box {css_class}">
            <h2>{title}</h2>
            {user_name_html}
            <p>{message}</p>
        </div>
        <p>Proceso completado.</p>
    </div>
</body>
</html>
    """
    return Response(html_content, mimetype='text/html')


# --- Inicialización de DB y Creación de Tablas ---

def init_db():
    """Crea las tablas, asegura las columnas necesarias y el usuario administrador si no existen."""
    db = None
    try:
        db = get_db()
        # 🚨 CAMBIO 4: Usar DictCursor de psycopg2 para cursores que devuelvan diccionarios
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # 1. Crear Tabla de Usuarios (con nuevos campos)
        # 🚨 CAMBIO 5: Sintaxis de PostgreSQL (SERIAL, UUID, TEXT en lugar de VARCHAR(80), etc.)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(80) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                qr_code_uuid UUID UNIQUE NULL, 
                first_name VARCHAR(100) NULL,           
                paternal_last_name VARCHAR(100) NULL,   
                maternal_last_name VARCHAR(100) NULL,   
                gender CHAR(1) NULL,                    
                phone_number VARCHAR(20) NULL           
            );
        ''')
        
        # ⚠️ Verificación de columnas (En PostgreSQL se hace diferente o se asume la creación inicial)
        # Para simplificar la migración y evitar dependencias de INFORMATION_SCHEMA,
        # confiamos en que CREATE TABLE IF NOT EXISTS maneja las columnas
        # y eliminamos el bloque de ALTER TABLE de MySQL.

        
        # 2. Crear Tabla de Asistencias 
        # 🚨 CAMBIO 6: Sintaxis de PostgreSQL (SERIAL, TIMESTAMP, REFERENCES)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL,
                check_in_time TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        ''')

        # 3. Agregar Usuario Administrador Inicial 
        admin_password_hash = generate_password_hash('adminpass')
        
        # 🚨 CAMBIO 7: Usar `WHERE username = %s` (psycopg2 usa %s)
        cursor.execute("SELECT id FROM users WHERE username = %s", ('admin',))
        admin_user = cursor.fetchone()  

        if admin_user is None:
            # Insertar admin con valores nulos/vacíos para los nuevos campos
            # 🚨 CAMBIO 8: Usar UUID para el UUID. Generar uno si es necesario para el admin, aunque aquí se usa NULL.
            # Se usa `NULL` para el UUID.
            cursor.execute(
                """
                INSERT INTO users (username, password, is_admin, qr_code_uuid, first_name, paternal_last_name, maternal_last_name, gender, phone_number) 
                VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s)
                """,
                ('admin', admin_password_hash, True, 'Admin', 'User', 'System', 'O', '000000000')
            )
            db.commit()  
            print("Usuario 'admin' creado con éxito en PostgreSQL.")
        else:
            print("El usuario 'admin' ya existe.")
            
    except Exception as e:
        print(f"Error en init_db: {e}")
        if db is not None:
            db.rollback() 
            
    finally:
        pass

# Inicializar la base de datos al arrancar
with app.app_context():
    init_db()

# --- Rutas de la Aplicación (Lógica Web) ---

@app.route('/')
def index():
    """Página principal. Redirecciona al login, al admin dashboard, o al QR del usuario."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    
    # Usuario normal es redirigido directamente a su QR
    return redirect(url_for('show_qr'))


# --- Rutas de Administración ---

@app.route('/admin')
def admin_dashboard():
    """Dashboard principal para el administrador."""
    if not session.get('is_admin'):
        return "Acceso denegado.", 403

    # Nota: Requiere que 'admin_dashboard.html' exista.
    return render_template('admin_dashboard.html')


@app.route('/admin/attendance')
def admin_attendance_report():
    """Muestra el reporte completo de asistencias, agrupado por día, con separación de string en Python."""
    if not session.get('is_admin'):
        return "Acceso denegado.", 403

    db = get_db()
    # 🚨 CAMBIO 9: Usar DictCursor para los fetchall
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Seleccionamos los campos de nombre, apellido y phone_number
    cursor.execute("""
        SELECT 
            u.first_name,
            u.paternal_last_name,
            u.maternal_last_name,
            u.phone_number,
            a.check_in_time 
        FROM attendance a
        JOIN users u ON a.user_id = u.id
        ORDER BY a.check_in_time DESC
    """)
    all_attendances = cursor.fetchall()

    attendances_by_day = defaultdict(list)
    
    for record in all_attendances:
        
        # Formatear el nombre completo: APELLIDO PATERNO, APELLIDO MATERNO, NOMBRES
        # Nota: La estructura del diccionario es la misma con DictCursor
        full_name = f"{record['paternal_last_name']} {record['maternal_last_name']}, {record['first_name']}"
        
        # Procesamiento de fecha y hora (check_in_time es un objeto datetime en Python)
        time_data_dt = record['check_in_time'] # Es un objeto datetime
        
        date_key = time_data_dt.strftime('%Y-%m-%d')
        time_str = time_data_dt.strftime('%H:%M:%S')

        if not date_key:
            continue

        attendances_by_day[date_key].append({
            'full_name': full_name,  # Usamos el nombre completo aquí
            'time': time_str,
            'phone_number': record.get('phone_number') 
        })
        
    # Nota: Requiere que 'admin_attendance.html' exista.
    return render_template('admin_attendance.html', attendances_by_day=attendances_by_day)


@app.route('/admin/attendance/individual', methods=['GET', 'POST'])
def admin_individual_report():
    """
    Permite buscar un usuario por nombre/apellido/teléfono y muestra un calendario.
    """
    if not session.get('is_admin'):
        return "Acceso denegado.", 403

    db = get_db()
    # 🚨 CAMBIO 10: Usar DictCursor
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    search_results = None
    user_data_for_calendar = None 
    total_attendance_count = 0 # <-- NUEVA VARIABLE: Inicializada a 0
    
    # 🚨 CAMBIO 11: Usar int() para forzar la conversión de user_id
    user_id_to_display = request.args.get('user_id') 

    if request.method == 'POST':
        search_term = request.form.get('search_term', '').strip()
        if search_term:
            # Búsqueda usando LIKE para encontrar coincidencias parciales
            search_query = f"%{search_term}%"
            cursor.execute(
                """
                SELECT id, first_name, paternal_last_name, maternal_last_name, phone_number
                FROM users
                WHERE is_admin = FALSE AND (
                    first_name ILIKE %s OR  
                    paternal_last_name ILIKE %s OR  
                    maternal_last_name ILIKE %s OR
                    phone_number LIKE %s
                )
                ORDER BY paternal_last_name, maternal_last_name, first_name
                """,
                # 🚨 CAMBIO 12: Usar ILIKE (Case Insensitive LIKE) en PostgreSQL
                (search_query, search_query, search_query, search_query)
            )
            search_results = cursor.fetchall()

    if user_id_to_display: 
        try:
            user_id = int(user_id_to_display)
            # Si hay un user_id en la URL, buscamos a ese usuario específico para mostrar su calendario
            cursor.execute(
                """
                SELECT id, first_name, paternal_last_name, maternal_last_name, phone_number
                FROM users
                WHERE id = %s
                """,
                (user_id,)
            )
            user_data_for_calendar = cursor.fetchone()
            
            # OBTENER EL CONTEO TOTAL DE ASISTENCIAS (Histórico)
            if user_data_for_calendar:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM attendance
                    WHERE user_id = %s
                    """,
                    (user_data_for_calendar['id'],)
                )
                # Guardamos el resultado en la nueva variable
                total_attendance_count = cursor.fetchone()['total']
        except ValueError:
            # Manejar el caso de que user_id_to_display no sea un entero
            user_id_to_display = None
            user_data_for_calendar = None
    
    # Se envían todas las variables a la plantilla
    return render_template('admin_individual_report.html', 
                            search_results=search_results,
                            user_id_to_display=user_id_to_display,
                            user_data_for_calendar=user_data_for_calendar,
                            total_attendance_count=total_attendance_count) 


@app.route('/api/attendance/user/<int:user_id>/<int:year>/<int:month>')
def get_individual_attendance(user_id, year, month):
    """
    Devuelve los días asistidos por un usuario y los días activos del sistema
    para un mes específico, incluyendo la hora de check-in del usuario.
    """
    if not session.get('is_admin'):
        return jsonify({"error": "Acceso denegado."}), 403

    db = None
    cursor = None
    try:
        db = get_db()
        # 🚨 CAMBIO 13: Usar DictCursor para los fetchall
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

        start_date = datetime.date(year, month, 1)
        if month == 12:
            end_date = datetime.date(year + 1, 1, 1)
        else:
            end_date = datetime.date(year, month + 1, 1)

        # 1. Obtener todas las asistencias del USUARIO
        cursor.execute(
            """
            SELECT check_in_time
            FROM attendance
            WHERE user_id = %s
              AND check_in_time >= %s
              AND check_in_time < %s
            ORDER BY check_in_time ASC
            """,
            (user_id, start_date, end_date)
        )
        
        # Almacenaremos la hora del primer check-in del día
        attended_records = {} 
        for record in cursor.fetchall():
            check_in_dt = record['check_in_time'] # Objeto datetime de Python
            date_key = check_in_dt.strftime('%Y-%m-%d')
            time_str = check_in_dt.strftime('%H:%M') # 🚨 Formato de hora (sin segundos para la visualización)
            
            # Solo se guarda el primer registro de la hora más temprana del día
            if date_key not in attended_records:
                attended_records[date_key] = time_str

        # 2. Obtener días activos del SISTEMA (Global)
        # 🚨 CAMBIO 14: Usar DATE() en PostgreSQL es check_in_time::DATE
        cursor.execute(
            """
            SELECT DISTINCT check_in_time::DATE as active_date
            FROM attendance
            WHERE check_in_time >= %s
              AND check_in_time < %s
            """,
            (start_date, end_date)
        )
        # El resultado de active_date es un objeto date en Python, se necesita formatear
        system_active_days = [record['active_date'].strftime('%Y-%m-%d') for record in cursor.fetchall() if record.get('active_date')]

        # 3. Respuesta del API
        return jsonify({
            "attended_days": list(attended_records.keys()),
            "attended_times": attended_records,  # 🚨 NUEVO DATO: { 'YYYY-MM-DD': 'HH:MM' }
            "system_active_days": system_active_days
        })

    except Exception as e:
        print(f"Error al obtener datos de asistencia: {e}")
        return jsonify({"error": "Error interno del servidor al obtener datos de asistencia."}), 500
    finally:
        if cursor:
            cursor.close()


# --- Rutas de Administración (Continuación) ---

@app.route('/admin/attendance/export/<int:user_id>/<int:year>/<int:month>')
def export_individual_attendance(user_id, year, month):
    """
    Genera un archivo CSV con la asistencia (ASISTIO/NO_ASISTIO/NADIE_ASISTIO)
    para un usuario en un mes específico, incluyendo la hora de registro.
    """
    if not session.get('is_admin'):
        return "Acceso denegado.", 403

    db = None
    cursor = None

    try:
        db = get_db()
        # 🚨 CAMBIO 15: Usar DictCursor para los fetchall
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Definir current_day_date para evitar errores de ámbito
        current_day_date = datetime.date.today()

        # 1. Obtener datos del usuario
        cursor.execute(
            "SELECT first_name, paternal_last_name, maternal_last_name FROM users WHERE id = %s",
            (user_id,)
        )
        user_data = cursor.fetchone()
        if not user_data:
            return "Usuario no encontrado.", 404
        
        full_name = f"{user_data['paternal_last_name']} {user_data['maternal_last_name']}, {user_data['first_name']}"
        
        # 2. Definir el rango de fechas
        start_date = datetime.date(year, month, 1)
        if month == 12:
            end_date = datetime.date(year + 1, 1, 1)
        else:
            end_date = datetime.date(year, month + 1, 1)

        # 3. Obtener asistencias registradas del USUARIO, incluyendo la hora
        cursor.execute(
            """
            SELECT check_in_time
            FROM attendance
            WHERE user_id = %s
              AND check_in_time >= %s
              AND check_in_time < %s
            ORDER BY check_in_time ASC
            """,
            (user_id, start_date, end_date)
        )
        
        attended_records = {}
        for record in cursor.fetchall():
            check_in_dt = record['check_in_time'] # Objeto datetime de Python
            date_key = check_in_dt.strftime('%Y-%m-%d')
            time_str = check_in_dt.strftime('%H:%M:%S')
            
            # Solo almacena el primer registro (hora más temprana) del día
            if date_key not in attended_records:
                attended_records[date_key] = time_str

        # Crear el set de días asistidos a partir de las claves del diccionario
        attended_dates = set(attended_records.keys())
        
        # 4. Obtener días activos del SISTEMA (Consulta Global)
        # 🚨 CAMBIO 16: Usar DATE() en PostgreSQL es check_in_time::DATE
        cursor.execute(
            """
            SELECT DISTINCT check_in_time::DATE as active_date
            FROM attendance
            WHERE check_in_time >= %s
              AND check_in_time < %s
            """,
            (start_date, end_date)
        )
        system_active_days = {record['active_date'].strftime('%Y-%m-%d') for record in cursor.fetchall() if record.get('active_date')}

        # 5. Construir el contenido del CSV en memoria
        output = io.StringIO()
        writer = csv.writer(output, delimiter=',')
        
        # Metadatos (Tildes eliminadas)
        writer.writerow(["REPORTE DE ASISTENCIA INDIVIDUAL", "", "", "", ""])
        writer.writerow(["EMPLEADO:", full_name, "MES:", f"{month}/{year}"])
        writer.writerow(["ASISTENCIAS PROPIAS:", str(len(attended_dates))])
        writer.writerow(["DIAS ACTIVOS DEL SISTEMA:", str(len(system_active_days))])
        writer.writerow([])
        
        # Encabezados (Nueva columna: Hora de Registro)
        header_row = ["Dia", "Dia Semana", "Fecha (YYYY-MM-DD)", "Hora de Registro", "Estado de Asistencia"]
        writer.writerow(header_row)

        days_in_month = (end_date - start_date).days
        # Corregir nombres de días de la semana (Lunes es 0 en Python)
        day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        
        # 6. Filas de datos
        for day in range(1, days_in_month + 1):
            current_date = datetime.date(year, month, day)
            date_key = current_date.strftime('%Y-%m-%d')
            
            day_of_week = current_date.weekday()
            day_name = day_names[day_of_week]
            
            is_user_attended = date_key in attended_dates
            is_system_active = date_key in system_active_days

            is_past_or_today = current_date <= current_day_date
            
            status = ""
            time_of_checkin = "" # Se inicializa la hora como vacía
            
            # 1. ASISTIO: Máxima prioridad.
            if is_user_attended:
                status = "ASISTIO"
                # Si asistió, se recupera la hora
                time_of_checkin = attended_records.get(date_key, "") 
            
            # 2. NO_ASISTIO: Sistema activo Y es pasado O HOY, pero el usuario NO asistió.
            elif is_system_active and is_past_or_today:
                status = "NO_ASISTIO"
            
            # 3. NADIE_ASISTIO: Si el sistema no estuvo activo o es un día futuro.
            else:
                status = "NADIE_ASISTIO"

            writer.writerow([
                day,
                day_name,
                date_key,
                time_of_checkin, # <-- Columna de la hora
                status
            ])

        # 7. Devolver el archivo CSV
        csv_output = output.getvalue()
        filename = f"reporte_asistencia_{user_id}_{year}_{month}_final.csv"
        
        response = Response(
            csv_output,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
        return response

    except Exception as e:
        print(f"ERROR AL GENERAR CSV: {e}")
        return f"Error interno del servidor al generar el CSV: {e}", 500
    finally:
        if cursor:
            cursor.close()


# --- NUEVAS RUTAS DE REPORTE INDIVIDUAL (FIN) ---


@app.route('/admin/scanner')
def admin_scanner():
    """Muestra la interfaz del escáner QR (solo para admin)."""
    if not session.get('is_admin'):
        return "Acceso denegado.", 403
    return render_template('admin_scanner.html')


# --- RUTA DE PRUEBA DE ESCÁNER ---
@app.route('/test_scanner')
def test_scanner_route():
    """Ruta para servir la página de prueba de carga de la librería y cámara."""
    return render_template('test_scanner.html')


# --- Rutas de Usuario y Asistencia ---

@app.route('/qrcode')
def show_qr():
    """Muestra el código QR único del usuario logueado."""
    if 'user_id' not in session or session.get('is_admin'):
        return redirect(url_for('index'))

    db = get_db()
    # 🚨 CAMBIO 17: Usar DictCursor
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT qr_code_uuid FROM users WHERE id = %s", (session.get('user_id'),))
    result = cursor.fetchone()
    # 🚨 CAMBIO 18: El UUID es un objeto UUID en Python, necesitamos convertirlo a string si no lo está.
    qr_uuid_obj = result.get('qr_code_uuid')
    qr_uuid = str(qr_uuid_obj) if qr_uuid_obj else None

    if not qr_uuid:
        return "Error: UUID no encontrado para el usuario.", 500

    # Genera la URL completa que contiene el UUID
    checkin_url = request.host_url.rstrip('/') + url_for('check_in', qr_uuid=qr_uuid)

    qr_base64 = generate_qr_code(checkin_url)

    # Nota: Requiere que 'qr_viewer.html' exista.
    return render_template('qr_viewer.html', qr_base64=qr_base64, checkin_url=checkin_url)


@app.route('/checkin/<qr_uuid>')
def check_in(qr_uuid):
    """Ruta que es accedida al escanear el código QR para registrar la asistencia."""
    
    # Asegúrate de que datetime y get_db estén definidos/importados en tu app.py
    
    db = get_db()
    # 🚨 CAMBIO 19: Usar DictCursor
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    message = ""
    status = "success"
    full_name = None # Inicializamos full_name

    # 1. Buscar usuario por UUID y obtener los campos de nombre
    cursor.execute(
        """
        SELECT 
            id, 
            username, 
            first_name, 
            paternal_last_name, 
            maternal_last_name 
        FROM users 
        WHERE qr_code_uuid = %s AND is_admin = FALSE
        """,  
        (qr_uuid,)
    )
    user_row = cursor.fetchone()

    if user_row is None:
        message = "Código QR inválido o el usuario es administrador."
        status = "error"
        # Si falla, full_name sigue siendo None, y el template lo manejará.
    else:
        user_id = user_row['id']
        username = user_row['username']
        
        # 💡 NUEVO CÓDIGO: Construir el nombre completo
        name_parts = [
            user_row.get('first_name', ''), 
            user_row.get('paternal_last_name', ''), 
            user_row.get('maternal_last_name', '')
        ]
        # Creamos el nombre completo, quitando espacios extra si faltan apellidos.
        full_name = " ".join(filter(None, name_parts)).strip()
        
        # Usamos el nombre completo para el mensaje, si existe, o el nombre de usuario por defecto
        display_name = full_name if full_name else username

        # 2. Verificar si ya registró asistencia hoy
        today = datetime.date.today()
        
        # 🚨 CAMBIO 20: Usar DATE() en PostgreSQL es check_in_time::DATE
        cursor.execute(
            """
            SELECT id FROM attendance 
            WHERE user_id = %s AND check_in_time::DATE = %s
            """,  
            (user_id, today.strftime('%Y-%m-%d'))
        )
        
        if cursor.fetchone() is not None:
            # 💡 MENSAJE MODIFICADO: Usamos el nombre completo (o display_name)
            message = f"¡Atención {display_name}! Ya registraste tu asistencia el día de hoy."
            status = "warning"
        else:
            try:
                # 3. Registrar la asistencia
                cursor.execute(
                    "INSERT INTO attendance (user_id) VALUES (%s)",
                    (user_id,)
                )
                db.commit()
                # 💡 MENSAJE MODIFICADO: Usamos el nombre completo (o display_name)
                message = f"¡Asistencia registrada con éxito para {display_name}!"
                status = "success"
            except Exception as e:
                db.rollback()
                message = f"Error al registrar asistencia: {e}"
                status = "error"

    # 4. Llamar a la función que genera el HTML, AHORA enviando el full_name
    return build_checkin_response(message, status, full_name)

# --- Rutas de Autenticación (Login/Register/Logout) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        # 🚨 CAMBIO 21: Usar DictCursor
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        error = None

        cursor.execute("SELECT id, password, is_admin, username FROM users WHERE username = %s", (username,))
        user_row = cursor.fetchone()

        if user_row is None:
            error = 'Nombre de usuario incorrecto.'
        else:
            if not check_password_hash(user_row['password'], password):
                error = 'Contraseña incorrecta.'

        if error is None:
            session.clear()
            session['user_id'] = user_row['id']
            session['username'] = user_row['username']
            session['is_admin'] = user_row['is_admin']
            return redirect(url_for('index'))
        
        return render_template('login.html', error=error)

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # 1. Datos de Acceso y Validación de Contraseña
        username = request.form['username']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        # 2. Nuevos Datos Personales
        first_name = request.form['first_name']
        paternal_last_name = request.form['paternal_last_name']
        maternal_last_name = request.form['maternal_last_name']
        gender = request.form['gender']
        # 🌟 CAMBIO 4: Captura el nuevo campo 'phone_number'
        phone_number = request.form.get('phone_number', '').strip() # Capturar y limpiar
        
        db = get_db()
        # 🚨 CAMBIO 22: Usar cursor simple para comandos de modificación
        cursor = db.cursor()
        error = None

        # 3. Validación (incluyendo el nuevo campo como opcional o con validación mínima)
        # Se asume que phone_number es opcional si solo se valida que los demás campos no sean vacíos.
        if not username or not password or not first_name or not paternal_last_name or not maternal_last_name or not gender:
            error = 'Todos los campos excepto el número de teléfono son requeridos.'
        
        # 🌟 NUEVA VALIDACIÓN: Si el número de teléfono no está vacío, puedes añadir una validación de formato
        if phone_number and not phone_number.isdigit():
             error = 'El número de teléfono debe contener solo dígitos.'
        
        elif password != confirm_password:
            error = 'Las contraseñas no coinciden. Por favor, repítela correctamente.'

        else:
            # 4. Verificar si el usuario ya existe
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cursor.fetchone() is not None:
                error = f"El usuario {username} ya está registrado."
        
        if error is None:
            try:
                # 5. Genera UUID y verifica unicidad
                max_attempts = 5
                qr_uuid = None
                
                for _ in range(max_attempts):
                    temp_uuid = str(uuid.uuid4())
                    cursor.execute("SELECT id FROM users WHERE qr_code_uuid = %s", (temp_uuid,))
                    if cursor.fetchone() is None:
                        qr_uuid = temp_uuid
                        break
                
                if qr_uuid is None:
                    raise Exception("Fallo al generar un UUID único después de varios intentos.")
                    
                
                hashed_password = generate_password_hash(password)
                
                # 6. Insertar el nuevo usuario con TODOS los campos
                # 🌟 CAMBIO 5: Se añade 'phone_number' al INSERT INTO y a la lista de valores
                cursor.execute(
                    """
                    INSERT INTO users (username, password, is_admin, qr_code_uuid, first_name, paternal_last_name, maternal_last_name, gender, phone_number) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (username, hashed_password, False, qr_uuid, first_name, paternal_last_name, maternal_last_name, gender, phone_number if phone_number else None)
                )
                db.commit()
                return redirect(url_for('login'))
            except Exception as e:
                db.rollback()
                error = f"Ocurrió un error al registrar el usuario: {e}"
        
        # Si hay error, se renderiza de nuevo con los datos enviados para no perderlos
        # Nota: Requiere que 'register.html' exista.
        return render_template('register.html', error=error)

    # Nota: Requiere que 'register.html' exista.
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- Ejecución de la Aplicación ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
