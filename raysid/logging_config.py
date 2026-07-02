"""
Logging configuration for RaySID client.

Provides a centralized logging setup with colored output and
configurable verbosity levels.

Format: [HH:MM:SS] [LEVEL] message
"""

import logging
import sys
from datetime import datetime
from typing import Optional


# ANSI Color codes
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    
    # Level colors
    DEBUG = '\033[36m'      # Cyan
    INFO = '\033[32m'       # Green
    WARNING = '\033[33m'    # Yellow
    ERROR = '\033[91m'      # Red
    CRITICAL = '\033[91m\033[1m'  # Bold Red


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colored output: [HH:MM:SS] [LEVEL] message"""
    
    LEVEL_COLORS = {
        'DEBUG': Colors.DEBUG,
        'INFO': Colors.INFO,
        'WARNING': Colors.WARNING,
        'ERROR': Colors.ERROR,
        'CRITICAL': Colors.CRITICAL,
    }
    
    LEVEL_NAMES = {
        'DEBUG': 'DEBUG',
        'INFO': 'INFO',
        'WARNING': 'WARN',
        'ERROR': 'ERROR',
        'CRITICAL': 'CRIT',
    }
    
    def format(self, record: logging.LogRecord) -> str:
        # Timestamp
        timestamp = datetime.now().strftime('%H:%M:%S')
        
        # Level with color and fixed width (pad to 5 chars for alignment)
        level_color = self.LEVEL_COLORS.get(record.levelname, '')
        level_name = self.LEVEL_NAMES.get(record.levelname, record.levelname)
        
        # Format the message
        formatted = f"[{timestamp}] [{level_color}{level_name}{Colors.RESET}] {record.getMessage()}"
        
        # Add exception info if present
        if record.exc_info:
            formatted += '\n' + self.formatException(record.exc_info)
        
        return formatted


class PlainFormatter(logging.Formatter):
    """Plain formatter for file logging: [YYYY-MM-DD HH:MM:SS] [LEVEL] [module] message"""
    
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        level = f"{record.levelname:8}"
        module = record.name.replace('raysid.', '')
        return f"[{timestamp}] [{level}] [{module}] {record.getMessage()}"


# Global logger instance
_root_logger: Optional[logging.Logger] = None


def setup_logging(
    verbose: bool = False,
    debug: bool = False,
    log_file: Optional[str] = None,
    quiet: bool = False
) -> logging.Logger:
    """
    Configure logging for the RaySID client.
    
    Args:
        verbose: Enable verbose output (INFO level)
        debug: Enable debug output (DEBUG level)
        log_file: Optional file path to write logs to
        quiet: Suppress all console output except errors
        
    Returns:
        The root logger for the raysid package
    """
    global _root_logger
    
    # Determine log level
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    elif quiet:
        level = logging.ERROR
    else:
        level = logging.INFO  # Default to INFO for user-friendly output
    
    # Get the root logger for our package
    logger = logging.getLogger('raysid')
    logger.setLevel(logging.DEBUG)  # Capture all, filter at handler level
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(PlainFormatter())
        logger.addHandler(file_handler)
    
    _root_logger = logger
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a specific module.
    
    Args:
        name: Module name (will be prefixed with 'raysid.')
        
    Returns:
        A logger instance
    """
    if not name.startswith('raysid.'):
        name = f'raysid.{name}'
    return logging.getLogger(name)


# Convenience functions
def get_client_logger() -> logging.Logger:
    """Get logger for the main client."""
    return get_logger('client')


def get_protocol_logger() -> logging.Logger:
    """Get logger for protocol/packet handling."""
    return get_logger('protocol')


def get_ble_logger() -> logging.Logger:
    """Get logger for BLE communication."""
    return get_logger('ble')


def get_spectrum_logger() -> logging.Logger:
    """Get logger for spectrum operations."""
    return get_logger('spectrum')


def get_audio_logger() -> logging.Logger:
    """Get logger for audio/tick sounds."""
    return get_logger('audio')
