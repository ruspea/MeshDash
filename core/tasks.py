import sqlite3
import sqlite3
import sqlite3
import sqlite3
import os
import logging
from fastapi import APIRouter, HTTPException, Response, status, Depends, Path, FastAPI
from pydantic import BaseModel, Field # Field can be used for more detailed validation if needed
from typing import List, Optional, Dict, Any
# No need for datetime import here as CURRENT_TIMESTAMP is handled by SQLite

# --- Configuration & Logging ---
# Default to tasks.db, but this will be overridden by init_tasks_db call from main app
DATABASE_FILE = "tasks.db" 
logger = logging.getLogger("meshtastic_dashboard.tasks") 

# --- Database Initialization ---
def init_tasks_db(db_path: str = None):
    """
    Initializes the tasks database and creates/updates the tasks table.
    If db_path is provided, it updates the global DATABASE_FILE.
    """
    global DATABASE_FILE
    if db_path:
        DATABASE_FILE = db_path
        
    logger.info(f"Initializing tasks database at: {DATABASE_FILE}")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        cursor = conn.cursor()

        # Create tasks table with the 'enabled' column
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nodeId TEXT NOT NULL,
                taskType TEXT NOT NULL,
                actionPayload TEXT,
                cronString TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                slotId TEXT DEFAULT 'node_0',
                createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("Tasks table created or already exists.")

        cursor.execute("PRAGMA table_info(tasks);")
        columns = [column[1] for column in cursor.fetchall()]
        if 'enabled' not in columns:
            logger.info("Attempting to add missing 'enabled' column to tasks table...")
            cursor.execute("ALTER TABLE tasks ADD COLUMN enabled BOOLEAN DEFAULT TRUE;")
            logger.info("'enabled' column added successfully to existing tasks table.")
        else:
            logger.debug("'enabled' column already exists in tasks table.")
        if 'slotId' not in columns:
            logger.info("Adding missing 'slotId' column to tasks table...")
            cursor.execute("ALTER TABLE tasks ADD COLUMN slotId TEXT DEFAULT 'node_0';")
            logger.info("'slotId' column added to existing tasks table.")

        # Trigger for updatedAt (remains the same)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS update_tasks_updatedAt
            AFTER UPDATE ON tasks
            FOR EACH ROW
            WHEN OLD.updatedAt = NEW.updatedAt OR OLD.updatedAt IS NULL -- Avoid recursion if updatedAt is explicitly set
            BEGIN
                UPDATE tasks SET updatedAt = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
        """)
        logger.info("UpdatedAt trigger for tasks table ensured.")

        conn.commit()
        logger.info("Tasks database initialization complete.")
    except sqlite3.Error as e:
        logger.exception(f"Tasks database initialization failed: {e}")
        # Propagate the error if initialization is critical for app startup
        raise
    finally:
        if conn:
            conn.close()

# --- Pydantic Models ---
class TaskBase(BaseModel):
    """Base model for task data, shared by create and update."""
    nodeId: str
    taskType: str
    actionPayload: Optional[str] = None
    cronString: str
    enabled: Optional[bool] = True
    slotId: Optional[str] = "node_0"

class TaskCreate(TaskBase):
    """Model for creating a new task. Inherits all fields from TaskBase."""
    pass

class TaskUpdate(BaseModel):
    """Model for updating an existing task. All fields are optional."""
    nodeId: Optional[str] = None
    taskType: Optional[str] = None
    actionPayload: Optional[str] = None
    cronString: Optional[str] = None
    enabled: Optional[bool] = None
    slotId: Optional[str] = None

class TaskInDB(TaskBase):
    """Model representing a task as stored in and retrieved from the database."""
    id: int
    createdAt: str
    updatedAt: str
    enabled: bool
    slotId: str = "node_0"

class ErrorResponse(BaseModel):
    """Standard error response model."""
    detail: str

class ApiSensorInfo(BaseModel): # This seems unrelated to tasks, but keeping it as it was in your original file
    """Model for sensor information (example)."""
    id: str
    name: str

# --- FastAPI Router ---
tasks_router = APIRouter()

# --- Database Dependency ---
def get_tasks_db_conn():
    """Helper function to get a database connection for the tasks DB."""
    try:
        # Ensure the database file directory exists if DB_PATH is more complex
        # For a simple filename, it will be created in the current working directory
        # or wherever the main app is running from if this path is relative.
        # db_dir = os.path.dirname(DATABASE_FILE)
        # if db_dir and not os.path.exists(db_dir):
        #     os.makedirs(db_dir, exist_ok=True)
        #     logger.info(f"Created directory for tasks database: {db_dir}")

        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        conn.row_factory = sqlite3.Row # Access columns by name
        # Enable WAL mode for better concurrency, if supported
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys = ON;") # Good practice if using foreign keys
        except sqlite3.Error as e:
            logger.warning(f"Could not set WAL journal mode or foreign_keys for tasks DB (might be unsupported): {e}")
        return conn
    except sqlite3.Error as e:
        logger.error(f"Tasks database connection error: {e}")
        # This exception will be caught by FastAPI and turned into a 500 error
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Tasks database connection error: {e}")

async def get_db():
    """FastAPI dependency to get a database connection and ensure it's closed."""
    conn = None
    try:
        conn = get_tasks_db_conn()
        yield conn
    finally:
        if conn:
            conn.close()

# --- API Endpoints ---

@tasks_router.post(
    "/",
    response_model=TaskInDB,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new scheduled task",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def create_task(task: TaskCreate, conn: sqlite3.Connection = Depends(get_db)):
    """
    Creates a new task entry in the database.
    The `enabled` field defaults to `True` if not provided.
    """
    try:
        cursor = conn.cursor()
        enabled_value = task.enabled if task.enabled is not None else True
        slot_id_value = task.slotId or 'node_0'

        cursor.execute(
            """
            INSERT INTO tasks (nodeId, taskType, actionPayload, cronString, enabled, slotId)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task.nodeId, task.taskType, task.actionPayload, task.cronString, enabled_value, slot_id_value)
        )
        conn.commit()
        new_task_id = cursor.lastrowid
        if new_task_id is None:
            logger.error("Failed to get ID of created task after commit (lastrowid is None).")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get ID of created task.")

        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, enabled, slotId, createdAt, updatedAt FROM tasks WHERE id = ?", (new_task_id,))
        created_task_row = cursor.fetchone()

        if created_task_row:
            created_task_dict = dict(created_task_row)
            logger.info(f"Created task with ID: {new_task_id}, Enabled: {created_task_dict.get('enabled')}, SlotId: {created_task_dict.get('slotId')}")
            return TaskInDB(**created_task_dict)
        else:
            logger.error(f"Failed to retrieve task {new_task_id} immediately after creation.")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve created task after insert.")

    except sqlite3.IntegrityError as e:
        logger.error(f"Database integrity error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database integrity error: {e}")
    except sqlite3.Error as e:
        logger.error(f"Database error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred: {e}")


@tasks_router.get(
    "/",
    response_model=List[TaskInDB],
    summary="Get all scheduled tasks",
    responses={500: {"model": ErrorResponse, "description": "Database error"}}
)
async def get_all_tasks(conn: sqlite3.Connection = Depends(get_db)):
    """
    Retrieves a list of all tasks currently stored in the database, including their 'enabled' status.
    """
    try:
        cursor = conn.cursor()
        # Ensure 'enabled' column is selected
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, enabled, slotId, createdAt, updatedAt FROM tasks ORDER BY createdAt DESC")
        tasks_rows = cursor.fetchall()
        # Convert each row to TaskInDB model
        tasks_list = [TaskInDB(**dict(row)) for row in tasks_rows]
        return tasks_list
    except sqlite3.Error as e:
        logger.error(f"Database error fetching all tasks: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}")


@tasks_router.get(
    "/{task_id}",
    response_model=TaskInDB,
    summary="Get a specific task by ID",
    responses={
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def get_task(task_id: int = Path(..., description="The ID of the task to retrieve"), conn: sqlite3.Connection = Depends(get_db)):
    """
    Retrieves the details of a single task specified by its unique ID.
    """
    try:
        cursor = conn.cursor()
        # Ensure 'enabled' column is selected
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, enabled, slotId, createdAt, updatedAt FROM tasks WHERE id = ?", (task_id,))
        task_row = cursor.fetchone()
        if task_row:
            task_dict = dict(task_row)
            return TaskInDB(**task_dict)
        else:
            logger.warning(f"Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")
    except sqlite3.Error as e:
        logger.error(f"Database error fetching task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}")


@tasks_router.put(
    "/{task_id}",
    response_model=TaskInDB,
    summary="Update an existing task",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input or no data to update"},
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def update_task(task_id: int, task_update_payload: TaskUpdate, conn: sqlite3.Connection = Depends(get_db)):
    """
    Updates an existing task specified by its ID. Partial updates are allowed.
    The `enabled` field can be updated.
    """
    try:
        cursor = conn.cursor()

        # Check if task exists
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        existing_task = cursor.fetchone()
        if not existing_task:
            logger.warning(f"Update failed: Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")

        # Prepare update data, excluding fields that were not set in the request
        try:
            # Pydantic v2
            update_data = task_update_payload.model_dump(exclude_unset=True)
        except AttributeError:
            # Pydantic v1
            update_data = task_update_payload.dict(exclude_unset=True)

        if not update_data:
            logger.warning(f"Update failed for task {task_id}: No update data provided.")
            # Return 400 if no actual data fields were sent for update
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided. At least one field must be specified for update.")

        # Construct the SET clause dynamically
        set_parts = []
        values = []
        for key, value in update_data.items():
            set_parts.append(f"{key} = ?")
            values.append(value)

        if not set_parts: # Should be caught by the 'if not update_data' above, but as a safeguard
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid fields to update.")

        set_clause = ", ".join(set_parts)
        # Add task_id to the end of values list for the WHERE clause
        values.append(task_id)

        query = f"UPDATE tasks SET {set_clause}, updatedAt = CURRENT_TIMESTAMP WHERE id = ?" # Explicitly update updatedAt

        cursor.execute(query, tuple(values))
        conn.commit()

        if cursor.rowcount == 0:
            # This might happen if the ID was valid but something went wrong during the UPDATE
            # or if the WHERE clause didn't match (though we checked existence).
            # More likely, it implies the data provided didn't change any values if all fields were identical.
            # However, since we are explicitly setting updatedAt, rowcount should be 1 if the ID exists.
            logger.warning(f"Task {task_id} update query executed but affected 0 rows. This is unexpected if the task exists.")
            # Re-fetch to confirm current state, or raise error if truly problematic.
            # For now, we proceed to fetch and return.

        # Fetch the updated task to return it
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, enabled, slotId, createdAt, updatedAt FROM tasks WHERE id = ?", (task_id,))
        updated_task_row = cursor.fetchone()

        if updated_task_row:
            updated_task_dict = dict(updated_task_row)
            logger.info(f"Updated task with ID: {task_id}. New enabled status: {updated_task_dict.get('enabled')}")
            return TaskInDB(**updated_task_dict)
        else:
            # This would be highly unusual if the update query didn't error and task existed.
            logger.error(f"Failed to retrieve task {task_id} after successful-looking update execution.")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve updated task after update execution.")

    except sqlite3.IntegrityError as e: # Should not occur with this schema unless a new unique constraint is added
        logger.error(f"Database integrity error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database integrity error: {e}")
    except sqlite3.Error as e:
        logger.error(f"Database error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}")
    except Exception as e: # Catch any other unexpected errors
        logger.error(f"Unexpected error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred: {e}")


@tasks_router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task by ID",
    responses={
        204: {"description": "Task deleted successfully"}, # Explicitly define 204
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def delete_task(task_id: int = Path(..., description="The ID of the task to delete"), conn: sqlite3.Connection = Depends(get_db)):
    """
    Deletes a task specified by its unique ID.
    Returns HTTP 204 No Content on successful deletion.
    """
    try:
        cursor = conn.cursor()

        # First, check if the task exists to provide a 404 if not
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        existing_task = cursor.fetchone()
        if not existing_task:
            logger.warning(f"Delete failed: Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")

        # If it exists, proceed with deletion
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

        # cursor.rowcount will be 1 if the delete was successful for an existing row.
        if cursor.rowcount == 0:
            # This case should ideally be caught by the existence check above.
            # If it happens, it means the task was deleted between the check and the delete operation (race condition, unlikely here).
            logger.warning(f"Task with ID {task_id} was not found for deletion, though it might have existed moments before.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found or already deleted.")
        else:
            logger.info(f"Deleted task with ID: {task_id}")
            # For 204, FastAPI expects no body, so return Response directly.
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    except sqlite3.Error as e:
        logger.error(f"Database error deleting task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}")
    except Exception as e: # Catch any other unexpected errors
        logger.error(f"Unexpected error deleting task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred: {e}")


# Example sensor endpoint (remains as it was, seems unrelated to primary task logic)
@tasks_router.get(
    "/sensors/{node_id}",
    response_model=List[ApiSensorInfo],
    summary="Get available sensors for a specific node (Hardcoded Example)",
    responses={
        500: {"model": ErrorResponse, "description": "Internal error (if real logic added later)"}
    },
    deprecated=True # Marking as deprecated if it's just an example
)
async def get_node_sensors_hardcoded(
    node_id: str = Path(..., description="Node ID string (e.g., !aabbccdd) - Currently unused in this hardcoded example")
):
    """
    Retrieves a list of available sensors reported by the node.
    **Note:** This endpoint currently returns a hardcoded list.
    """
    logger.info(f"API request for hardcoded sensors - Node ID '{node_id}' received but unused.")
    sensors = [
        ApiSensorInfo(id="bed_temp", name="Bedroom Temp"),
        ApiSensorInfo(id="hall_motion", name="Hallway Motion"),
    ]
    return sensors