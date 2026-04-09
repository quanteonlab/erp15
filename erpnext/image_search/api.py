"""
Image Search API Endpoints
Provides HTTP API for image search functionality
"""

import frappe
from frappe import _


@frappe.whitelist()
def enqueue_product_image_search(product_type, product_id, priority="Normal"):
    """
    Enqueue a product for image search

    Args:
        product_type: "Item" or "Product Approval Queue"
        product_id: Product identifier
        priority: "Critical", "High", "Normal", or "Low"

    Returns:
        Success response with job name
    """
    from erpnext.image_search.queue_manager import ImageSearchQueueManager

    queue_manager = ImageSearchQueueManager()
    job_name = queue_manager.enqueue_product(product_type, product_id, priority)

    if job_name:
        # Kick off the background worker so the job is actually processed
        from erpnext.image_search.worker import start_worker
        start_worker()
        return {
            "success": True,
            "job_name": job_name,
            "message": _("Product queued for image search")
        }
    else:
        return {
            "success": False,
            "message": _("Product already queued or job exists")
        }


@frappe.whitelist()
def get_product_image_candidates(product_type, product_id):
    """
    Get image candidates for a product

    Args:
        product_type: "Item" or "Product Approval Queue"
        product_id: Product identifier

    Returns:
        List of image candidates
    """
    candidates = frappe.get_all(
        "Product Image Candidate",
        filters={
            "product_type": product_type,
            "product_id": product_id
        },
        fields=["*"],
        order_by="rank ASC"
    )

    return candidates


@frappe.whitelist()
def select_primary_image(product_type, product_id, candidate_name):
    """
    Select a candidate as the primary product image

    Args:
        product_type: "Item" or "Product Approval Queue"
        product_id: Product identifier
        candidate_name: Name of the candidate to select

    Returns:
        Success response
    """
    # Unselect all current selections for this product
    frappe.db.sql("""
        UPDATE `tabProduct Image Candidate`
        SET is_selected = 0
        WHERE product_type = %s AND product_id = %s
    """, (product_type, product_id))

    # Select the new one
    frappe.db.set_value(
        "Product Image Candidate",
        candidate_name,
        "is_selected",
        1
    )

    # Update product with image
    candidate = frappe.get_doc("Product Image Candidate", candidate_name)

    if product_type == "Item":
        frappe.db.set_value("Item", product_id, "image", candidate.image_url)
    elif product_type == "Product Approval Queue":
        if frappe.db.exists("DocType", "Product Approval Queue"):
            frappe.db.set_value("Product Approval Queue", product_id, "image", candidate.image_url)

    frappe.db.commit()

    return {
        "success": True,
        "message": _("Primary image updated"),
        "image_url": candidate.image_url
    }


@frappe.whitelist()
def bulk_enqueue_products(product_type="Item", filters=None, priority="Low"):
    """
    Bulk enqueue products for image search

    Args:
        product_type: "Item" or "Product Approval Queue"
        filters: Filter dict (JSON string)
        priority: Job priority

    Returns:
        Summary of enqueued products
    """
    from erpnext.image_search.queue_manager import ImageSearchQueueManager
    import json

    if isinstance(filters, str):
        filters = json.loads(filters)

    queue_manager = ImageSearchQueueManager()
    result = queue_manager.bulk_enqueue_existing_products(
        product_type=product_type,
        filters=filters,
        priority=priority
    )

    return result


@frappe.whitelist()
def scan_and_enqueue_missing_images():
    """
    Scan all products for missing images and enqueue them

    Returns:
        Summary of scan results
    """
    from erpnext.image_search.queue_manager import ImageSearchQueueManager

    queue_manager = ImageSearchQueueManager()
    result = queue_manager.scan_and_enqueue_missing_images()

    return result


@frappe.whitelist()
def enqueue_items_without_images_and_candidates(priority="Low", limit=None):
    """
    Enqueue Item rows that have no image and no stored candidate options.

    Args:
        priority: Job priority level
        limit: Optional max number of items to enqueue

    Returns:
        Summary of queued products
    """
    from erpnext.image_search.queue_manager import ImageSearchQueueManager
    from erpnext.image_search.worker import start_worker

    queue_manager = ImageSearchQueueManager()
    result = queue_manager.enqueue_items_without_images_and_candidates(
        priority=priority,
        limit=limit,
    )

    if result.get("queued_count"):
        start_worker()

    return result


@frappe.whitelist()
def get_job_status(job_name):
    """
    Get status of a specific job

    Args:
        job_name: Job identifier

    Returns:
        Job status details
    """
    job = frappe.get_doc("Product Image Search Job", job_name)
    return {
        "name": job.name,
        "product_type": job.product_type,
        "product_id": job.product_id,
        "product_name": job.product_name,
        "status": job.status,
        "priority": job.priority,
        "images_found": job.images_found,
        "attempt_count": job.attempt_count,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at
    }


@frappe.whitelist()
def get_queue_stats():
    """
    Get overall queue statistics

    Returns:
        Queue stats including job counts and API usage
    """
    from datetime import datetime, timedelta

    stats = {
        "pending": frappe.db.count("Product Image Search Job", {"status": "Pending"}),
        "queued": frappe.db.count("Product Image Search Job", {"status": "Queued"}),
        "in_progress": frappe.db.count("Product Image Search Job", {"status": "In Progress"}),
        "completed": frappe.db.count("Product Image Search Job", {"status": "Completed"}),
        "failed": frappe.db.count("Product Image Search Job", {"status": "Failed"}),
        "retrying": frappe.db.count("Product Image Search Job", {"status": "Retrying"})
    }

    # API usage stats (last 24 hours)
    yesterday = datetime.now() - timedelta(days=1)

    stats["api_calls_24h"] = {
        "duckduckgo": frappe.db.count(
            "Image Search API Log",
            {
                "api_provider": "DuckDuckGo Images",
                "request_timestamp": [">=", yesterday]
            }
        ),
        "google": frappe.db.count(
            "Image Search API Log",
            {
                "api_provider": "Google Custom Search",
                "request_timestamp": [">=", yesterday]
            }
        ),
        "bing": frappe.db.count(
            "Image Search API Log",
            {
                "api_provider": "Bing Image Search",
                "request_timestamp": [">=", yesterday]
            }
        )
    }

    # Products without images
    stats["items_without_images"] = frappe.db.count(
        "Item",
        {"image": ["is", "not set"]}
    )

    if frappe.db.exists("DocType", "Product Approval Queue"):
        stats["paq_without_images"] = frappe.db.count(
            "Product Approval Queue",
            {"image": ["is", "not set"]}
        )
    else:
        stats["paq_without_images"] = 0

    stats["total_without_images"] = stats["items_without_images"] + stats["paq_without_images"]

    return stats


@frappe.whitelist()
def get_product_jobs_ui(product_type, status_group="running", limit=100):
    """
    Get image search jobs for UI display.

    Args:
        product_type: "Item" or "Product Approval Queue"
        status_group: "running" or "done"
        limit: max rows to return

    Returns:
        Dict with jobs list and requested group
    """
    try:
        row_limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        row_limit = 100

    group = (status_group or "running").strip().lower()
    if group == "done":
        statuses = ["Completed", "Failed"]
        order_by = "IFNULL(completed_at, modified) DESC"
    else:
        group = "running"
        statuses = ["Pending", "Queued", "In Progress", "Retrying"]
        order_by = "modified DESC"

    jobs = frappe.get_all(
        "Product Image Search Job",
        filters={
            "product_type": product_type,
            "status": ["in", statuses],
        },
        fields=[
            "name",
            "product_type",
            "product_id",
            "product_name",
            "status",
            "priority",
            "images_found",
            "attempt_count",
            "error_message",
            "created_at",
            "started_at",
            "completed_at",
            "modified",
        ],
        order_by=order_by,
        limit_page_length=row_limit,
    )

    return {
        "status_group": group,
        "jobs": jobs,
    }


@frappe.whitelist()
def clear_product_jobs_ui(product_type, include_failed=0):
    """
    Clear finalized image-search jobs for one product type.

    Args:
        product_type: "Item" or "Product Approval Queue"
        include_failed: truthy value to also clear Failed jobs

    Returns:
        Dict with deleted count and statuses removed
    """
    statuses = ["Completed"]
    if frappe.utils.cint(include_failed):
        statuses.append("Failed")

    names = frappe.get_all(
        "Product Image Search Job",
        filters={
            "product_type": product_type,
            "status": ["in", statuses],
        },
        pluck="name",
        limit_page_length=0,
    )

    if not names:
        return {
            "deleted_count": 0,
            "statuses": statuses,
            "product_type": product_type,
        }

    for name in names:
        frappe.delete_doc(
            "Product Image Search Job",
            name,
            ignore_permissions=True,
            delete_permanently=True,
        )

    frappe.db.commit()

    return {
        "deleted_count": len(names),
        "statuses": statuses,
        "product_type": product_type,
    }


@frappe.whitelist()
def trigger_worker():
    """
    Manually trigger the background worker

    Returns:
        Success response
    """
    from erpnext.image_search.worker import start_worker

    start_worker()

    return {
        "success": True,
        "message": _("Image search worker triggered")
    }


@frappe.whitelist()
def get_product_images_ui(product_type, product_id):
    """
    Get image candidates with additional UI data for display

    Args:
        product_type: "Item" or "Product Approval Queue"
        product_id: Product identifier

    Returns:
        Dict with candidates and job status
    """
    # Hydrate legacy static results into DB on first read when needed.
    if product_type == "Item" and not frappe.db.exists(
        "Product Image Candidate",
        {"product_type": "Item", "product_id": product_id},
    ):
        from erpnext.image_search.legacy_candidate_import import import_candidates_for_item

        import_candidates_for_item(product_id)

    # Get candidates
    candidates = get_product_image_candidates(product_type, product_id)

    # Get job status (if exists)
    job = frappe.get_all(
        "Product Image Search Job",
        filters={
            "product_type": product_type,
            "product_id": product_id
        },
        fields=["name", "status", "images_found", "error_message"],
        order_by="creation DESC",
        limit=1
    )

    return {
        "candidates": candidates,
        "job": job[0] if job else None,
        "total_candidates": len(candidates),
        "has_selection": any(c.get("is_selected") for c in candidates)
    }
