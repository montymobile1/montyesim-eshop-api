import os
import sqlite3

# SMELL: Hardcoded credentials (Security Hotspot)
DB_PASSWORD = "supersecretpassword123!" 

def process_data(user_input):
    """
    SMELL: High Cognitive Complexity (too many nested blocks)
    SMELL: Broad Exception Handling (Too broad catch)
    """
    try:
        if user_input:
            if isinstance(user_input, str):
                if len(user_input) > 5:
                    # SMELL: Use of eval() (Security Risk)
                    data = eval(user_input) 
                    return data
        else:
            # SMELL: Unused variable
            unused_var = 10
            return None
    except Exception as e:
        # SMELL: Print instead of logging
        print("Error occurred") 
        return None

def db_query(user_id):
    """
    SMELL: SQL Injection vulnerability
    """
    conn = sqlite3.connect('example.db')
    cursor = conn.cursor()
    # SMELL: String formatting in SQL query
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchall()

# SMELL: Missing docstrings
def function_with_no_docstring():
    x = 1
    y = 2
    # SMELL: Magic number
    return x + y + 100 

if __name__ == "__main__":
    # SMELL: Code duplication
    print(process_data("123"))
    print(process_data("123"))