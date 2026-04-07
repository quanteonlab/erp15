"""
Hook Handlers for Image Search Worker
Auto-enqueue products when created or image removed
"""

import frappe


def enqueue_item_image_search(doc, method):
    """
    Auto-enqueue new items for image search if they have no image
    Hook: Item.after_insert
    """
    settings = frappe.get_single("Image Search Settings")

    if not settings.worker_enabled or not settings.auto_enqueue_new_products:
        return

    # Only enqueue if item has no image
    if not doc.image:
        from erpnext.image_search.queue_manager import ImageSearchQueueManager

        queue_manager = ImageSearchQueueManager()
        queue_manager.enqueue_product(
            product_type="Item",
            product_id=doc.name,
            priority="Normal"
        )
        frappe.logger().info(f"Auto-enqueued Item {doc.name} for image search")


def check_item_image_removed(doc, method):
    """
    Check if image was removed from item and re-enqueue
    Hook: Item.on_update
    """
    settings = frappe.get_single("Image Search Settings")

    if not settings.worker_enabled or not settings.auto_enqueue_new_products:
        return

    # Check if image was removed
    if doc.has_value_changed("image") and not doc.image:
        from erpnext.image_search.queue_manager import ImageSearchQueueManager

        queue_manager = ImageSearchQueueManager()
        queue_manager.enqueue_product(
            product_type="Item",
            product_id=doc.name,
            priority="Low"
        )
        frappe.logger().info(f"Re-enqueued Item {doc.name} - image was removed")


def enqueue_approval_queue_image_search(doc, method):
    """
    Auto-enqueue products in approval queue
    Hook: Product Approval Queue.after_insert
    """
    settings = frappe.get_single("Image Search Settings")

    if not settings.worker_enabled or not settings.auto_enqueue_new_products:
        return

    from erpnext.image_search.queue_manager import ImageSearchQueueManager

    queue_manager = ImageSearchQueueManager()
    queue_manager.enqueue_product(
        product_type="Product Approval Queue",
        product_id=doc.name,
        priority="High"  # Higher priority for approval queue
    )
    frappe.logger().info(f"Auto-enqueued Product Approval Queue {doc.name} for image search")
