# Logging Configuration

## Environment Variables

### LOG_LEVEL
Controls the overall logging level for the application.
- **Default**: `WARNING`
- **Options**: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

### DEBUG_MODE  
Enables verbose session-specific logging for debugging.
- **Default**: `false`
- **Options**: `true`, `false`

## Usage Examples

### Production (Minimal Logging)
```bash
LOG_LEVEL=WARNING
DEBUG_MODE=false
```

### Development (Verbose Logging)
```bash
LOG_LEVEL=INFO
DEBUG_MODE=true
```

### Debug Session Issues
```bash
LOG_LEVEL=DEBUG
DEBUG_MODE=true
```

## What Gets Logged

### Always (regardless of DEBUG_MODE)
- Errors and warnings
- Session stop/start events
- Critical system events

### Only when DEBUG_MODE=true
- "Worker processing session: {session_id}"
- "Worker skipped stopped session: {session_id}"  
- "Broadcasting session: {session_id}"
- "Dropped stopped session: {session_id}"

## Docker Compose Example

```yaml
services:
  backend:
    environment:
      - LOG_LEVEL=WARNING
      - DEBUG_MODE=false
  
  processing:
    environment:
      - LOG_LEVEL=WARNING  
      - DEBUG_MODE=false
```

This configuration provides a clean production log while maintaining the ability to enable detailed debugging when needed.
