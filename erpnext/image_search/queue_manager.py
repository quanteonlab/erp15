"""
Image Search Queue Manager
Manages the queue of image search jobs for products
"""

import frappe
from datetime import datetime, timedelta
from typing import List, Dict, Optional


class ImageSearchQueueManager:
    """Manages the queue of image search jobs"""

    PRIORITY_ORDER = {
        'Critical': 1,
        'High': 2,
        'Normal': 3,
        'Low': 4
    }

    def __init__(self):
        self.settings = frappe.get_single("Image Search Settings")

    def enqueue_product(self, product_type: str, product_id: str, priority: str = "Normal"):
        """
        Add a product to the image search queue

        Args:
            product_type: 'Item' or 'Product Approval Queue'
            product_id: Product identifier
            priority: Job priority level

        Returns:
            Job name if created, None if already exists
        """
        # Check if job already exists
        existing_job = frappe.db.exists({
            "doctype": "Product Image Search Job",
            "product_type": product_type,
            "product_id": product_id,
            "status": ["in", ["Pending", "Queued", "In Progress", "Retrying"]]
        })

        if existing_job:
            frappe.log_error(
                f"Job already exists for {product_type} {product_id}",
                "Image Search Queue - Duplicate"
            )
            return existing_job

        # Get product details for search query
        search_query = self._generate_search_query(product_type, product_id)
        product_name = self._get_product_name(product_type, product_id)

        # Create job
        job = frappe.get_doc({
            "doctype": "Product Image Search Job",
            "product_type": product_type,
            "product_id": product_id,
            "product_name": product_name,
            "search_query": search_query,
            "priority": priority,
            "status": "Pending",
            "target_count": self.settings.target_image_count or 6,
            "created_at": frappe.utils.now()
        })
        job.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(f"Enqueued image search for {product_type} {product_id}")
        return job.name

    def _get_product_name(self, product_type: str, product_id: str) -> str:
        """Get product display name"""
        try:
            if product_type == "Item":
                return frappe.db.get_value("Item", product_id, "item_name") or product_id
            elif product_type == "Product Approval Queue":
                return frappe.db.get_value("Product Approval Queue", product_id, "normalized_title") or product_id
        except Exception:
            return product_id

        return product_id

    def _generate_search_query(self, product_type: str, product_id: str) -> str:
        """Generate optimized search query from product data"""
        try:
            if product_type == "Item":
                item = frappe.get_doc("Item", product_id)
                brand = item.brand or ""
                name = item.item_name or item.item_code
                return f"{brand} {name}".strip()

            elif product_type == "Product Approval Queue":
                product = frappe.get_doc("Product Approval Queue", product_id)
                brand = product.brand or ""
                title = product.normalized_title or product.source_title
                return f"{brand} {title}".strip()
        except Exception as e:
            frappe.log_error(f"Error generating search query: {str(e)}")
            return product_id

        return product_id

    def get_next_batch(self, batch_size: int = 10) -> List[Dict]:
        """
        Get next batch of jobs to process

        Returns jobs ordered by:
        1. Priority (Critical > High > Normal > Low)
        2. Created date (older first)
        """
        # Check if worker is enabled
        if not self.settings.worker_enabled:
            return []

        # Check API rate limits first (if using paid APIs)
        # DuckDuckGo is free so we skip this for now

        raw_jobs = frappe.get_all(
            "Product Image Search Job",
            filters={"status": ["in", ["Pending", "Retrying"]]},
            fields=["name", "product_type", "product_id", "product_name",
                    "search_query", "priority", "attempt_count", "max_attempts", "next_retry_at", "status"],
            order_by="CASE priority "
                     "WHEN 'Critical' THEN 1 "
                     "WHEN 'High' THEN 2 "
                     "WHEN 'Normal' THEN 3 "
                     "WHEN 'Low' THEN 4 END, "
                     "created_at ASC",
            limit_page_length=max(batch_size * 4, batch_size)
        )

        now_dt = frappe.utils.now_datetime()
        jobs = []
        for job in raw_jobs:
            attempts = int(job.get("attempt_count") or 0)
            max_attempts = int(job.get("max_attempts") or 3)
            if attempts >= max_attempts:
                continue

            if job.get("status") == "Retrying":
                next_retry_at = job.get("next_retry_at")
                if next_retry_at:
                    retry_dt = frappe.utils.get_datetime(next_retry_at)
                    if retry_dt and retry_dt > now_dt:
                        continue

            jobs.append(job)
            if len(jobs) >= batch_size:
                break

        # Mark as queued
        for job in jobs:
            frappe.db.set_value(
                "Product Image Search Job",
                job.name,
                "status",
                "Queued"
            )

        frappe.db.commit()

        return jobs

    def mark_job_started(self, job_name: str):
        """Mark job as in progress"""
        frappe.db.set_value(
            "Product Image Search Job",
            job_name,
            {
                "status": "In Progress",
                "started_at": frappe.utils.now()
            }
        )
        frappe.db.commit()

    def mark_job_completed(self, job_name: str, images_found: int):
        """Mark job as completed"""
        frappe.db.set_value(
            "Product Image Search Job",
            job_name,
            {
                "status": "Completed",
                "completed_at": frappe.utils.now(),
                "images_found": images_found
            }
        )
        frappe.db.commit()

    def mark_job_failed(self, job_name: str, error_message: str):
        """Mark job as failed with retry logic"""
        job = frappe.get_doc("Product Image Search Job", job_name)
        job.attempt_count += 1
        job.error_message = error_message

        if job.attempt_count >= job.max_attempts:
            job.status = "Failed"
            frappe.logger().error(
                f"Job {job_name} failed after {job.attempt_count} attempts: {error_message}"
            )
        else:
            job.status = "Retrying"
            # Exponential backoff: 5min, 15min, 45min
            retry_delay = 5 * (3 ** (job.attempt_count - 1))
            job.next_retry_at = frappe.utils.add_to_date(
                None, minutes=retry_delay
            )
            frappe.logger().info(
                f"Job {job_name} will retry in {retry_delay} minutes (attempt {job.attempt_count})"
            )

        job.save(ignore_permissions=True)
        frappe.db.commit()

    def bulk_enqueue_existing_products(self,
                                      product_type: str = "Item",
                                      filters: Dict = None,
                                      priority: str = "Low"):
        """
        Bulk enqueue existing products for image search

        Example:
            # All items without images
            bulk_enqueue_existing_products(
                product_type="Item",
                filters={"image": ["is", "not set"]},
                priority="Normal"
            )
        """
        filters = filters or {}

        if product_type == "Item":
            doctype = "Item"
        else:
            doctype = "Product Approval Queue"

        products = frappe.get_all(
            doctype,
            filters=filters,
            fields=["name"],
            limit=None
        )

        job_names = []
        for product in products:
            try:
                job_name = self.enqueue_product(
                    product_type=product_type,
                    product_id=product.name,
                    priority=priority
                )
                if job_name:
                    job_names.append(job_name)
            except Exception as e:
                frappe.log_error(f"Error enqueueing {product.name}: {str(e)}")

        frappe.logger().info(
            f"Bulk enqueued {len(job_names)} products out of {len(products)} total"
        )

        return {
            "queued_count": len(job_names),
            "total_products": len(products),
            "job_names": job_names
        }

    def enqueue_items_without_images_and_candidates(self,
                                                    priority: str = "Low",
                                                    limit: Optional[int] = None):
        """
        Enqueue Item rows that still have no image and no stored candidates.

        This is intended for the POS jobs modal so operators can retry products
        that still show "No image" and have no candidate options available.
        """
        limit_clause = ""
        values: List = []
        if limit is not None:
            try:
                limit_value = max(1, int(limit))
            except (TypeError, ValueError):
                limit_value = 100
            limit_clause = " LIMIT %s"
            values.append(limit_value)

        products = frappe.db.sql(
            """
            SELECT i.name
            FROM `tabItem` i
            WHERE (i.image IS NULL OR i.image = '')
              AND NOT EXISTS (
                SELECT 1
                FROM `tabProduct Image Candidate` pic
                WHERE pic.product_type = 'Item'
                  AND pic.product_id = i.name
              )
              AND NOT EXISTS (
                SELECT 1
                FROM `tabProduct Image Search Job` job
                WHERE job.product_type = 'Item'
                  AND job.product_id = i.name
                  AND job.status IN ('Pending', 'Queued', 'In Progress', 'Retrying')
              )
            ORDER BY i.modified DESC
            """ + limit_clause,
            tuple(values),
            as_dict=True,
        )

        job_names = []
        for product in products:
            try:
                job_name = self.enqueue_product(
                    product_type="Item",
                    product_id=product.name,
                    priority=priority,
                )
                if job_name:
                    job_names.append(job_name)
            except Exception as e:
                frappe.log_error(f"Error enqueueing {product.name}: {str(e)}")

        return {
            "queued_count": len(job_names),
            "eligible_count": len(products),
            "job_names": job_names,
        }

    def scan_and_enqueue_missing_images(self):
        """
        Scan for products without images and enqueue them
        Called by scheduler to automatically find products needing images
        """
        if not self.settings.process_existing_items:
            return

        # Scan Items without images
        items_enqueued = self.bulk_enqueue_existing_products(
            product_type="Item",
            filters={"image": ["is", "not set"]},
            priority="Low"
        )

        # Scan Product Approval Queue without images (if doctype exists)
        paq_enqueued = {"queued_count": 0, "total_products": 0}
        if frappe.db.exists("DocType", "Product Approval Queue"):
            paq_enqueued = self.bulk_enqueue_existing_products(
                product_type="Product Approval Queue",
                filters={"image": ["is", "not set"]},
                priority="High"  # Higher priority for approval queue
            )

        total_enqueued = items_enqueued["queued_count"] + paq_enqueued["queued_count"]
        total_found = items_enqueued["total_products"] + paq_enqueued["total_products"]

        frappe.logger().info(
            f"Scanned and enqueued {total_enqueued} products out of {total_found} without images"
        )

        return {
            "items": items_enqueued,
            "approval_queue": paq_enqueued,
            "total_enqueued": total_enqueued,
            "total_found": total_found
        }
