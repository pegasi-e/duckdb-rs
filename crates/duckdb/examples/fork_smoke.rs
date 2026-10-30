use duckdb::{Connection, Result, params};

fn main() -> Result<()> {
    let connection = Connection::open_in_memory()?;
    let (version, source_id): (String, String) =
        connection.query_row("SELECT library_version, source_id FROM pragma_version()", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?;
    assert_eq!(version, "v1.5.6");
    assert_eq!(source_id, "d5adf13edc1");
    connection.execute_batch("CREATE TABLE smoke (id INTEGER PRIMARY KEY, name VARCHAR)")?;
    connection.execute("INSERT INTO smoke VALUES (?, ?)", params![1, "fork"])?;
    {
        let mut appender = connection.appender("smoke")?;
        appender.append_row(params![2, "bundled"])?;
        appender.flush()?;
    }
    let count: i64 = connection.query_row("SELECT count(*) FROM smoke", [], |row| row.get(0))?;
    assert_eq!(count, 2);
    println!("DuckDB {version} ({source_id}): query, prepared insert and appender passed");
    Ok(())
}
