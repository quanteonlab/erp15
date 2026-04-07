"""
Image Search Scheduler
Scheduled tasks for image search worker
"""

import frappe


def run_image_search_worker():
    """
    Scheduled task to run image search worker
    Runs every 15 minutes (configurable in Image Search Settings)
    """
    settings = frappe.get_single("Image Search Settings")

    if not settings.worker_enabled:
        frappe.logger().info("Image search worker is disabled")
        return

    from erpnext.image_search.worker import start_worker

    frappe.logger().info("Starting scheduled image search worker")
    start_worker()


def scan_products_without_images():
    """
    Scheduled task to scan for products without images and enqueue them
    Runs daily to catch new products without images
    """
    settings = frappe.get_single("Image Search Settings")

    if not settings.worker_enabled or not settings.process_existing_items:
        return

    from erpnext.image_search.queue_manager import ImageSearchQueueManager

    frappe.logger().info("Scanning for products without images")
    queue_manager = ImageSearchQueueManager()
    result = queue_manager.scan_and_enqueue_missing_images()

    frappe.logger().info(
        f"Scan complete: {result['total_enqueued']} products enqueued "
        f"out of {result['total_found']} without images"
    )
