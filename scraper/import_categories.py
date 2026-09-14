"""
scripts/import_categories.py
Importa categorías de torneos desde CSV con manejo robusto de errores.
"""
import os
import csv
import time
import logging
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

CSV_PATH = Path("tournaments_2025_2026_categorized.csv")
VALID_CATEGORIES = {
    'gs', '1000', 'atp500', 'atp250',
    'ch175', 'ch125', 'ch100', 'ch75', 'ch50',
    'futures', 'utr', 'other'
}


def get_connection():
    """Obtiene conexión a la base de datos con timeout."""
    try:
        db_url = os.environ.get("SUPABASE_DB_URL")
        if not db_url:
            raise ValueError("SUPABASE_DB_URL no está configurada en el archivo .env")
        
        logger.info("Conectando a la base de datos...")
        conn = psycopg2.connect(db_url, connect_timeout=10)
        logger.info("✅ Conexión establecida correctamente")
        return conn
    except psycopg2.Error as e:
        logger.error(f"❌ Error de conexión a PostgreSQL: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Error inesperado al conectar: {e}")
        raise


def validate_csv_file(csv_path: Path):
    """Valida que el archivo CSV exista y sea legible."""
    if not csv_path.exists():
        raise FileNotFoundError(f"❌ El archivo CSV no existe: {csv_path}")
    
    if not csv_path.is_file():
        raise ValueError(f"❌ La ruta no es un archivo: {csv_path}")
    
    if csv_path.stat().st_size == 0:
        raise ValueError(f"❌ El archivo CSV está vacío: {csv_path}")
    
    logger.info(f"✅ Archivo CSV encontrado: {csv_path} ({csv_path.stat().st_size} bytes)")


def read_csv_data(csv_path: Path):
    """Lee y valida los datos del CSV."""
    logger.info(f"Leyendo datos del CSV...")
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            # Validar que tenga las columnas necesarias
            required_columns = {'source_tournament_id', 'category'}
            if not required_columns.issubset(set(reader.fieldnames or [])):
                missing = required_columns - set(reader.fieldnames or [])
                raise ValueError(f"❌ El CSV no tiene las columnas requeridas: {missing}")
            
            data = []
            invalid_categories = []
            
            for row_num, row in enumerate(reader, 1):
                slug = row.get('source_tournament_id', '').strip()
                category = row.get('category', '').strip().lower()
                
                if not slug:
                    logger.warning(f"Fila {row_num}: slug vacío, saltando")
                    continue
                
                if category and category not in VALID_CATEGORIES:
                    invalid_categories.append((slug, category, row_num))
                    category = 'other'  # Usar 'other' como default
                
                data.append({
                    'slug': slug,
                    'category': category or 'other'
                })
            
            logger.info(f"✅ {len(data)} registros válidos leídos del CSV")
            
            if invalid_categories:
                logger.warning(f"⚠️  {len(invalid_categories)} categorías inválidas encontradas (usando 'other'):")
                for slug, cat, row in invalid_categories[:5]:  # Mostrar solo los primeros 5
                    logger.warning(f"   Fila {row}: {slug} -> '{cat}'")
                if len(invalid_categories) > 5:
                    logger.warning(f"   ... y {len(invalid_categories) - 5} más")
            
            return data
            
    except UnicodeDecodeError as e:
        logger.error(f"❌ Error de encoding en el CSV: {e}")
        raise
    except csv.Error as e:
        logger.error(f"❌ Error al procesar el CSV: {e}")
        raise


def add_category_column(conn):
    """Añade la columna category si no existe."""
    logger.info("Paso 1: Verificando columna 'category'...")
    
    try:
        cur = conn.cursor()
        
        # Verificar si la columna existe
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'tournaments' AND column_name = 'category'
        """)
        
        if cur.fetchone():
            logger.info("✅ La columna 'category' ya existe")
        else:
            logger.info("Creando columna 'category'...")
            cur.execute("ALTER TABLE tournaments ADD COLUMN category TEXT")
            conn.commit()
            logger.info("✅ Columna 'category' creada exitosamente")
        
        cur.close()
        
    except psycopg2.Error as e:
        logger.error(f"❌ Error al modificar la tabla: {e}")
        conn.rollback()
        raise


def update_categories(conn, data):
    """Actualiza las categorías en la base de datos."""
    logger.info(f"Paso 2: Actualizando categorías ({len(data)} registros)...")
    
    try:
        cur = conn.cursor()
        
        updated = 0
        not_found = 0
        errors = 0
        
        start_time = time.time()
        
        for idx, item in enumerate(data, 1):
            slug = item['slug']
            category = item['category']
            
            try:
                cur.execute(
                    "UPDATE tournaments SET category = %s WHERE source_tournament_id = %s",
                    (category, slug)
                )
                
                if cur.rowcount > 0:
                    updated += 1
                else:
                    not_found += 1
                
                # Log de progreso cada 100 registros
                if idx % 100 == 0:
                    elapsed = time.time() - start_time
                    rate = idx / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"   Progreso: {idx}/{len(data)} "
                        f"({idx/len(data)*100:.1f}%) - "
                        f"{rate:.1f} registros/seg"
                    )
                
            except psycopg2.Error as e:
                errors += 1
                logger.error(f"Error al actualizar {slug}: {e}")
                conn.rollback()
                continue
        
        conn.commit()
        cur.close()
        
        elapsed = time.time() - start_time
        
        logger.info("✅ Actualización completada")
        logger.info(f"   📊 Resumen:")
        logger.info(f"      • Actualizados: {updated}")
        logger.info(f"      • No encontrados en BD: {not_found}")
        logger.info(f"      • Errores: {errors}")
        logger.info(f"      • Tiempo total: {elapsed:.2f} segundos")
        
        return updated, not_found, errors
        
    except Exception as e:
        logger.error(f"❌ Error inesperado durante la actualización: {e}")
        conn.rollback()
        raise


def generate_report(conn, updated, not_found, errors):
    """Genera un reporte final de la importación."""
    logger.info("Paso 3: Generando reporte...")
    
    try:
        cur = conn.cursor()
        
        # Contar categorías en la BD
        cur.execute("""
            SELECT category, COUNT(*) as count
            FROM tournaments
            WHERE category IS NOT NULL
            GROUP BY category
            ORDER BY count DESC
        """)
        
        categories = cur.fetchall()
        
        logger.info("📊 Distribución de categorías en la base de datos:")
        for category, count in categories:
            logger.info(f"   • {category}: {count} torneos")
        
        # Contar torneos sin categoría
        cur.execute("""
            SELECT COUNT(*) 
            FROM tournaments 
            WHERE category IS NULL
        """)
        
        without_category = cur.fetchone()[0]
        
        if without_category > 0:
            logger.warning(f"⚠️  {without_category} torneos aún sin categoría")
            
            # Mostrar algunos ejemplos
            cur.execute("""
                SELECT source_tournament_id, name
                FROM tournaments
                WHERE category IS NULL
                LIMIT 10
            """)
            
            examples = cur.fetchall()
            if examples:
                logger.info("   Ejemplos:")
                for slug, name in examples:
                    logger.info(f"      • {slug}: {name}")
        
        cur.close()
        
        # Guardar reporte en archivo
        report_path = Path("logs/category_import_report.txt")
        report_path.parent.mkdir(exist_ok=True)
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"REPORTE DE IMPORTACIÓN DE CATEGORÍAS\n")
            f.write(f"Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*60}\n\n")
            f.write(f"Resumen:\n")
            f.write(f"  • Actualizados: {updated}\n")
            f.write(f"  • No encontrados: {not_found}\n")
            f.write(f"  • Errores: {errors}\n\n")
            f.write(f"Distribución de categorías:\n")
            for category, count in categories:
                f.write(f"  • {category}: {count}\n")
            f.write(f"\nTorneos sin categoría: {without_category}\n")
        
        logger.info(f"✅ Reporte guardado en: {report_path}")
        
    except Exception as e:
        logger.error(f"❌ Error al generar el reporte: {e}")


def main():
    """Función principal con manejo completo de errores."""
    logger.info("=" * 60)
    logger.info("INICIANDO IMPORTACIÓN DE CATEGORÍAS")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    try:
        # Validar archivo CSV
        validate_csv_file(CSV_PATH)
        
        # Leer datos del CSV
        data = read_csv_data(CSV_PATH)
        
        if not data:
            logger.warning("⚠️  No hay datos válidos en el CSV")
            return
        
        # Conectar a la base de datos
        conn = get_connection()
        
        try:
            # Paso 1: Añadir columna
            add_category_column(conn)
            
            # Paso 2: Actualizar categorías
            updated, not_found, errors = update_categories(conn, data)
            
            # Paso 3: Generar reporte
            generate_report(conn, updated, not_found, errors)
            
        finally:
            conn.close()
            logger.info("✅ Conexión a la base de datos cerrada")
        
        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"✅ IMPORTACIÓN COMPLETADA EN {elapsed:.2f} SEGUNDOS")
        logger.info("=" * 60)
        
    except FileNotFoundError as e:
        logger.error(str(e))
        logger.info("💡 Asegúrate de que el archivo CSV esté en la raíz del proyecto")
    except ValueError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Importación cancelada por el usuario")
    except Exception as e:
        logger.error(f"❌ Error inesperado: {e}")
        logger.exception("Detalles del error:")


if __name__ == "__main__":
    main()