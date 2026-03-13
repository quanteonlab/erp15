# E-Commerce Integration API for ERPNext

Complete REST API integration layer for connecting e-commerce sites with ERPNext ERP system.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Authentication](#authentication)
- [API Endpoints](#api-endpoints)
  - [Product/Item APIs](#productitem-apis)
  - [Inventory/Stock APIs](#inventorystock-apis)
  - [Customer APIs](#customer-apis)
  - [Order APIs](#order-apis)
  - [Payment APIs](#payment-apis)
  - [Coupon/Pricing APIs](#couponpricing-apis)
  - [Shipping/Delivery APIs](#shippingdelivery-apis)
  - [Invoice APIs](#invoice-apis)
  - [Utility APIs](#utility-apis)
- [Integration Guide](#integration-guide)
- [Webhooks](#webhooks)
- [Error Handling](#error-handling)
- [Examples](#examples)

---

## Overview

This integration provides a comprehensive set of REST API endpoints that allow e-commerce websites to seamlessly integrate with ERPNext for:

- Product catalog synchronization
- Real-time inventory management
- Order processing and fulfillment
- Customer data synchronization
- Payment processing
- Shipping and delivery tracking
- Invoice generation

## Features

- **Product Management**: Sync products, variants, pricing, and attributes
- **Real-time Inventory**: Check stock levels, reserve inventory, update quantities
- **Order Processing**: Create orders, track status, manage fulfillment
- **Customer Sync**: Create/update customers, manage addresses and contacts
- **Payment Integration**: Process payments, apply payment methods
- **Pricing & Discounts**: Dynamic pricing rules, coupon code validation
- **Shipping**: Create delivery notes, track shipments
- **Invoicing**: Generate invoices from orders
- **Multi-company Support**: Handle multiple companies and warehouses

---

## Installation

### 1. Enable API Access in ERPNext

```bash
# Navigate to your bench directory
cd /path/to/frappe-bench

# Install the integration (if creating as separate app)
# Or simply use the built-in module
bench --site your-site-name migrate
```

### 2. Create API User and Generate API Keys

1. Go to **User** in ERPNext
2. Create a new user (e.g., "wordpress_api")
3. Assign appropriate roles:
   - Sales User
   - Stock User
   - Accounts User
4. Generate API Key and Secret:
   - Go to User > API Access
   - Generate Keys
   - Save the **API Key** and **API Secret** securely

### 3. Configure WordPress Plugin

Install a WordPress REST API client or create custom integration code to call ERPNext APIs.

---

## Authentication

All API endpoints require authentication using ERPNext's token-based authentication.

### Using API Key and Secret

```bash
# Example using cURL
curl -X GET \
  'https://your-erpnext-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products' \
  -H 'Authorization: token <api_key>:<api_secret>' \
  -H 'Content-Type: application/json'
```

### Using Session Token

```python
import requests

# Login to get session
login_url = "https://your-erpnext-site.com/api/method/login"
credentials = {
    "usr": "your_username",
    "pwd": "your_password"
}
session = requests.Session()
response = session.post(login_url, data=credentials)

# Use session for subsequent requests
products = session.get(
    "https://your-erpnext-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products"
)
```

---

## API Endpoints

### Base URL

```
https://your-erpnext-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api
```

---

## Product/Item APIs

### 1. Get Products (with pagination)

**Endpoint:** `/get_products`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| filters | JSON/dict | No | Additional filters for Item doctype |
| fields | list | No | List of fields to return |
| start | int | No | Pagination offset (default: 0) |
| page_length | int | No | Records per page (default: 20) |
| order_by | str | No | Sort order (default: "modified desc") |
| search_term | str | No | Search in item_code, item_name, description |
| item_group | str | No | Filter by item group |
| price_list | str | No | Price list to fetch prices from |

**Response:**

```json
{
  "message": {
    "items": [
      {
        "name": "ITEM-001",
        "item_code": "PROD-001",
        "item_name": "Sample Product",
        "description": "Product description",
        "item_group": "Products",
        "stock_uom": "Nos",
        "is_stock_item": 1,
        "has_variants": 0,
        "image": "/files/product.jpg",
        "standard_rate": 100.0,
        "price_list_rate": 99.0,
        "stock_qty": 50
      }
    ],
    "total_count": 150,
    "has_more": true
  }
}
```

**Example:**

```bash
curl -X GET \
  'https://your-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products?price_list=Standard%20Selling&page_length=50' \
  -H 'Authorization: token <api_key>:<api_secret>'
```

---

### 2. Get Product Details

**Endpoint:** `/get_product`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| item_code | str | Yes | Item code or item name |
| price_list | str | No | Price list to fetch price from |
| warehouse | str | No | Warehouse to check stock from |
| customer | str | No | Customer for customer-specific pricing |

**Response:**

```json
{
  "message": {
    "name": "ITEM-001",
    "item_code": "PROD-001",
    "item_name": "Sample Product",
    "description": "Detailed product description",
    "item_group": "Products",
    "brand": "Brand Name",
    "stock_uom": "Nos",
    "is_stock_item": 1,
    "has_variants": 0,
    "variant_of": null,
    "image": "/files/product.jpg",
    "standard_rate": 100.0,
    "price_list_rate": 99.0,
    "stock_qty": 50,
    "projected_qty": 45,
    "attributes": [],
    "variants": [],
    "all_prices": [
      {
        "price_list": "Standard Selling",
        "price_list_rate": 99.0,
        "currency": "USD"
      }
    ]
  }
}
```

**Example:**

```python
import requests

url = "https://your-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_product"
headers = {
    "Authorization": "token <api_key>:<api_secret>"
}
params = {
    "item_code": "PROD-001",
    "price_list": "Standard Selling",
    "warehouse": "Main Store"
}

response = requests.get(url, headers=headers, params=params)
product = response.json()["message"]
```

---

### 3. Get Item Variants

**Endpoint:** `/get_item_variants`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| item_code | str | Yes | Template item code |

**Response:**

```json
{
  "message": [
    {
      "name": "ITEM-001-RED",
      "item_code": "PROD-001-RED",
      "item_name": "Sample Product - Red",
      "image": "/files/product-red.jpg",
      "standard_rate": 100.0
    },
    {
      "name": "ITEM-001-BLUE",
      "item_code": "PROD-001-BLUE",
      "item_name": "Sample Product - Blue",
      "image": "/files/product-blue.jpg",
      "standard_rate": 100.0
    }
  ]
}
```

---

### 4. Search Items

**Endpoint:** `/search_items`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| search_term | str | Yes | Search query |
| price_list | str | No | Price list to include pricing |
| limit | int | No | Maximum results (default: 20) |

**Response:**

```json
{
  "message": [
    {
      "name": "ITEM-001",
      "item_code": "PROD-001",
      "item_name": "Sample Product",
      "description": "Product description",
      "image": "/files/product.jpg",
      "standard_rate": 100.0,
      "price_list_rate": 99.0
    }
  ]
}
```

---

## Inventory/Stock APIs

### 1. Get Stock Balance

**Endpoint:** `/get_stock_balance`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| item_code | str | Yes | Item code |
| warehouse | str | No | Warehouse name (returns total if not provided) |

**Response:**

```json
{
  "message": 50.0
}
```

**Example:**

```javascript
// JavaScript/Node.js
const axios = require('axios');

async function checkStock(itemCode, warehouse) {
  const response = await axios.get(
    'https://your-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_stock_balance',
    {
      params: { item_code: itemCode, warehouse: warehouse },
      headers: { 'Authorization': 'token <api_key>:<api_secret>' }
    }
  );

  return response.data.message;
}

// Usage
const stock = await checkStock('PROD-001', 'Main Store');
console.log(`Available stock: ${stock}`);
```

---

### 2. Check Stock Availability (Batch)

**Endpoint:** `/check_stock_availability`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| items | list | Yes | List of dicts with item_code and qty |
| warehouse | str | No | Warehouse to check |

**Request Body:**

```json
{
  "items": [
    {"item_code": "PROD-001", "qty": 5},
    {"item_code": "PROD-002", "qty": 10}
  ],
  "warehouse": "Main Store"
}
```

**Response:**

```json
{
  "message": [
    {
      "item_code": "PROD-001",
      "requested_qty": 5.0,
      "available_qty": 50.0,
      "is_available": true
    },
    {
      "item_code": "PROD-002",
      "requested_qty": 10.0,
      "available_qty": 3.0,
      "is_available": false
    }
  ]
}
```

---

### 3. Update Stock

**Endpoint:** `/update_stock`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| item_code | str | Yes | Item code |
| warehouse | str | Yes | Target warehouse |
| qty | float | Yes | Quantity to add (positive) or remove (negative) |
| posting_date | str | No | Date for stock entry (default: today) |

**Request Body:**

```json
{
  "item_code": "PROD-001",
  "warehouse": "Main Store",
  "qty": 100,
  "posting_date": "2025-10-30"
}
```

**Response:**

```json
{
  "message": {
    "stock_entry": "STE-00001",
    "item_code": "PROD-001",
    "warehouse": "Main Store",
    "new_qty": 150.0
  }
}
```

---

## Customer APIs

### 1. Get Customer

**Endpoint:** `/get_customer`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| customer_name | str | No* | Customer name/ID |
| email | str | No* | Customer email address |

*Either customer_name or email is required

**Response:**

```json
{
  "message": {
    "name": "CUST-00001",
    "customer_name": "John Doe",
    "customer_type": "Individual",
    "customer_group": "Individual",
    "territory": "United States",
    "email_id": "john@example.com",
    "addresses": [
      {
        "name": "ADDR-00001",
        "address_line1": "123 Main St",
        "city": "New York",
        "state": "NY",
        "pincode": "10001",
        "country": "United States",
        "address_type": "Billing"
      }
    ],
    "contacts": [
      {
        "name": "CONT-00001",
        "email_id": "john@example.com",
        "phone": "+1234567890"
      }
    ]
  }
}
```

---

### 2. Create Customer

**Endpoint:** `/create_customer`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| customer_name | str | Yes | Customer name |
| email | str | No | Email address |
| phone | str | No | Phone number |
| customer_group | str | No | Customer group (default: Individual) |
| territory | str | No | Territory (default: All Territories) |
| customer_type | str | No | Customer type (default: Individual) |

**Request Body:**

```json
{
  "customer_name": "John Doe",
  "email": "john@example.com",
  "phone": "+1234567890",
  "customer_group": "Individual",
  "territory": "United States"
}
```

**Response:**

```json
{
  "message": {
    "name": "CUST-00001",
    "customer_name": "John Doe",
    "customer_type": "Individual",
    "customer_group": "Individual",
    "territory": "United States"
  }
}
```

---

### 3. Create Address

**Endpoint:** `/create_address`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| customer_name | str | Yes | Customer name/ID |
| address_line1 | str | Yes | Address line 1 |
| city | str | Yes | City |
| country | str | No | Country (default: United States) |
| address_type | str | No | Address type (default: Billing) |
| address_line2 | str | No | Address line 2 |
| state | str | No | State |
| pincode | str | No | PIN/ZIP code |
| email | str | No | Email for this address |
| phone | str | No | Phone for this address |
| is_primary | int | No | Mark as primary (default: 0) |
| is_shipping | int | No | Mark as shipping (default: 0) |

**Request Body:**

```json
{
  "customer_name": "CUST-00001",
  "address_line1": "123 Main St",
  "address_line2": "Apt 4B",
  "city": "New York",
  "state": "NY",
  "pincode": "10001",
  "country": "United States",
  "address_type": "Shipping",
  "phone": "+1234567890",
  "is_shipping": 1
}
```

**Response:**

```json
{
  "message": {
    "name": "ADDR-00001",
    "address_line1": "123 Main St",
    "city": "New York",
    "state": "NY",
    "country": "United States"
  }
}
```

---

## Order APIs

### 1. Create Order

**Endpoint:** `/create_order`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| customer | str | Yes | Customer name/ID |
| items | list | Yes | List of items with item_code, qty, rate |
| order_type | str | No | Order type (default: "Shopping Cart") |
| delivery_date | str | No | Expected delivery date |
| company | str | No | Company name |
| currency | str | No | Transaction currency |
| price_list | str | No | Price list to use |
| shipping_address | str | No | Shipping address name |
| billing_address | str | No | Billing address name |
| taxes | list | No | List of tax charges |
| payment_terms | str | No | Payment terms template |
| coupon_code | str | No | Coupon/promo code |

**Request Body:**

```json
{
  "customer": "CUST-00001",
  "items": [
    {
      "item_code": "PROD-001",
      "qty": 2,
      "rate": 99.0
    },
    {
      "item_code": "PROD-002",
      "qty": 1,
      "rate": 149.0
    }
  ],
  "order_type": "Shopping Cart",
  "price_list": "Standard Selling",
  "shipping_address": "ADDR-00001",
  "billing_address": "ADDR-00002",
  "coupon_code": "SAVE10"
}
```

**Response:**

```json
{
  "message": {
    "name": "SO-00001",
    "customer": "CUST-00001",
    "transaction_date": "2025-10-30",
    "delivery_date": "2025-11-06",
    "status": "Draft",
    "grand_total": 337.0,
    "currency": "USD",
    "items": [
      {
        "item_code": "PROD-001",
        "qty": 2.0,
        "rate": 99.0,
        "amount": 198.0
      }
    ]
  }
}
```

---

### 2. Get Order

**Endpoint:** `/get_order`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| order_name | str | Yes | Sales order name/ID |

**Response:**

```json
{
  "message": {
    "name": "SO-00001",
    "customer": "CUST-00001",
    "customer_name": "John Doe",
    "transaction_date": "2025-10-30",
    "delivery_date": "2025-11-06",
    "status": "To Deliver and Bill",
    "grand_total": 337.0,
    "currency": "USD",
    "items": [...],
    "taxes": [...]
  }
}
```

---

### 3. Update Order Status

**Endpoint:** `/update_order_status`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| order_name | str | Yes | Sales order name/ID |
| status | str | Yes | New status (Cancelled, Closed, Completed) |

**Request Body:**

```json
{
  "order_name": "SO-00001",
  "status": "Cancelled"
}
```

---

### 4. Get Customer Orders

**Endpoint:** `/get_customer_orders`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| customer | str | Yes | Customer name/ID |
| start | int | No | Pagination offset (default: 0) |
| page_length | int | No | Records per page (default: 20) |

**Response:**

```json
{
  "message": {
    "orders": [
      {
        "name": "SO-00001",
        "transaction_date": "2025-10-30",
        "delivery_date": "2025-11-06",
        "status": "To Deliver and Bill",
        "grand_total": 337.0,
        "currency": "USD",
        "order_type": "Shopping Cart"
      }
    ],
    "total_count": 5,
    "has_more": false
  }
}
```

---

## Payment APIs

### 1. Create Payment

**Endpoint:** `/create_payment`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| payment_type | str | Yes | "Receive" or "Pay" |
| party | str | Yes | Customer or Supplier name |
| amount | float | Yes | Payment amount |
| payment_method | str | No | Mode of payment (default: Cash) |
| reference_no | str | No | External payment reference |
| reference_date | str | No | Payment date |
| reference_doctype | str | No | Reference document type |
| reference_name | str | No | Reference document name |
| company | str | No | Company name |

**Request Body:**

```json
{
  "payment_type": "Receive",
  "party": "CUST-00001",
  "amount": 337.0,
  "payment_method": "Credit Card",
  "reference_no": "TXN-12345",
  "reference_doctype": "Sales Order",
  "reference_name": "SO-00001"
}
```

**Response:**

```json
{
  "message": {
    "name": "PE-00001",
    "payment_type": "Receive",
    "party": "CUST-00001",
    "paid_amount": 337.0,
    "status": "Submitted"
  }
}
```

---

### 2. Get Payment Methods

**Endpoint:** `/get_payment_methods`
**Method:** GET
**Auth:** Required

**Response:**

```json
{
  "message": [
    {
      "name": "Cash",
      "mode_of_payment": "Cash",
      "type": "Cash"
    },
    {
      "name": "Credit Card",
      "mode_of_payment": "Credit Card",
      "type": "Bank"
    }
  ]
}
```

---

## Coupon/Pricing APIs

### 1. Validate Coupon

**Endpoint:** `/validate_coupon`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| coupon_code | str | Yes | Coupon code to validate |
| customer | str | No | Customer name |
| items | list | No | List of items to apply coupon on |

**Response:**

```json
{
  "message": {
    "valid": true,
    "coupon_code": "SAVE10",
    "pricing_rule": "10% Off",
    "discount_percentage": 10.0,
    "discount_amount": 0.0,
    "message": "Coupon code is valid"
  }
}
```

---

### 2. Apply Coupon to Order

**Endpoint:** `/apply_coupon_to_order`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| order_name | str | Yes | Sales order name |
| coupon_code | str | Yes | Coupon code |

**Request Body:**

```json
{
  "order_name": "SO-00001",
  "coupon_code": "SAVE10"
}
```

---

## Shipping/Delivery APIs

### 1. Create Delivery Note

**Endpoint:** `/create_delivery_note`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| sales_order | str | Yes | Sales order name |

**Request Body:**

```json
{
  "sales_order": "SO-00001"
}
```

**Response:**

```json
{
  "message": {
    "name": "DN-00001",
    "customer": "CUST-00001",
    "posting_date": "2025-10-30",
    "status": "Draft",
    "items": [...]
  }
}
```

---

### 2. Update Tracking Info

**Endpoint:** `/update_tracking_info`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| delivery_note | str | Yes | Delivery note name |
| tracking_number | str | Yes | Tracking number |
| carrier | str | No | Carrier name |

**Request Body:**

```json
{
  "delivery_note": "DN-00001",
  "tracking_number": "1Z999AA10123456784",
  "carrier": "UPS"
}
```

---

## Invoice APIs

### 1. Create Invoice

**Endpoint:** `/create_invoice`
**Method:** POST
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| sales_order | str | No* | Sales order name |
| delivery_note | str | No* | Delivery note name |

*Either sales_order or delivery_note is required

**Request Body:**

```json
{
  "sales_order": "SO-00001"
}
```

**Response:**

```json
{
  "message": {
    "name": "SI-00001",
    "customer": "CUST-00001",
    "posting_date": "2025-10-30",
    "due_date": "2025-11-13",
    "status": "Draft",
    "grand_total": 337.0,
    "outstanding_amount": 337.0
  }
}
```

---

### 2. Get Invoice

**Endpoint:** `/get_invoice`
**Method:** GET
**Auth:** Required

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| invoice_name | str | Yes | Sales invoice name |

**Response:**

```json
{
  "message": {
    "name": "SI-00001",
    "customer": "CUST-00001",
    "posting_date": "2025-10-30",
    "due_date": "2025-11-13",
    "status": "Unpaid",
    "grand_total": 337.0,
    "outstanding_amount": 337.0,
    "items": [...],
    "taxes": [...]
  }
}
```

---

## Utility APIs

### 1. Get Item Groups

**Endpoint:** `/get_item_groups`
**Method:** GET
**Auth:** Required

**Response:**

```json
{
  "message": [
    {
      "name": "Products",
      "parent_item_group": "All Item Groups",
      "is_group": 1,
      "image": null
    }
  ]
}
```

---

### 2. Get Price Lists

**Endpoint:** `/get_price_lists`
**Method:** GET
**Auth:** Required

**Response:**

```json
{
  "message": [
    {
      "name": "Standard Selling",
      "currency": "USD",
      "price_not_uom_dependent": 0
    }
  ]
}
```

---

### 3. Get Warehouses

**Endpoint:** `/get_warehouses`
**Method:** GET
**Auth:** Required

**Response:**

```json
{
  "message": [
    {
      "name": "Main Store",
      "warehouse_name": "Main Store",
      "parent_warehouse": null,
      "company": "Your Company"
    }
  ]
}
```

---

### 4. Health Check

**Endpoint:** `/ping`
**Method:** GET
**Auth:** Not Required

**Response:**

```json
{
  "message": {
    "status": "ok",
    "message": "ERPNext WordPress WooCommerce API is running",
    "frappe_version": "15.0.0",
    "site": "your-site.com"
  }
}
```

---

## Integration Guide

### WordPress Plugin Development

Here's a sample WordPress plugin structure for integrating with ERPNext:

```php
<?php
/**
 * Plugin Name: ERPNext WooCommerce Integration
 * Description: Integrates WooCommerce with ERPNext ERP
 * Version: 1.0.0
 */

class ERPNext_Integration {

    private $api_url;
    private $api_key;
    private $api_secret;

    public function __construct() {
        $this->api_url = get_option('erpnext_api_url');
        $this->api_key = get_option('erpnext_api_key');
        $this->api_secret = get_option('erpnext_api_secret');

        // Hooks
        add_action('woocommerce_new_order', array($this, 'sync_order_to_erp'));
        add_action('woocommerce_product_set_stock', array($this, 'sync_stock_from_erp'));
    }

    /**
     * Make API request to ERPNext
     */
    private function api_request($endpoint, $method = 'GET', $data = array()) {
        $url = $this->api_url . '/' . $endpoint;

        $args = array(
            'method' => $method,
            'headers' => array(
                'Authorization' => 'token ' . $this->api_key . ':' . $this->api_secret,
                'Content-Type' => 'application/json'
            )
        );

        if ($method === 'POST' && !empty($data)) {
            $args['body'] = json_encode($data);
        } elseif ($method === 'GET' && !empty($data)) {
            $url = add_query_arg($data, $url);
        }

        $response = wp_remote_request($url, $args);

        if (is_wp_error($response)) {
            error_log('ERPNext API Error: ' . $response->get_error_message());
            return false;
        }

        $body = wp_remote_retrieve_body($response);
        return json_decode($body, true);
    }

    /**
     * Sync WooCommerce order to ERPNext
     */
    public function sync_order_to_erp($order_id) {
        $order = wc_get_order($order_id);

        // Prepare items
        $items = array();
        foreach ($order->get_items() as $item) {
            $product = $item->get_product();
            $items[] = array(
                'item_code' => $product->get_sku(),
                'qty' => $item->get_quantity(),
                'rate' => $item->get_total() / $item->get_quantity()
            );
        }

        // Prepare order data
        $order_data = array(
            'customer' => $this->get_or_create_customer($order),
            'items' => $items,
            'order_type' => 'Shopping Cart',
            'price_list' => 'Standard Selling'
        );

        // Create order in ERPNext
        $result = $this->api_request(
            'erpnext.erpnext_integrations.ecommerce_api.api.create_order',
            'POST',
            $order_data
        );

        if ($result && isset($result['message']['name'])) {
            // Save ERPNext order ID in WooCommerce
            update_post_meta($order_id, '_erpnext_order_id', $result['message']['name']);
            $order->add_order_note('Order synced to ERPNext: ' . $result['message']['name']);
        }
    }

    /**
     * Get or create customer in ERPNext
     */
    private function get_or_create_customer($order) {
        $email = $order->get_billing_email();

        // Try to get existing customer
        $customer = $this->api_request(
            'erpnext.erpnext_integrations.ecommerce_api.api.get_customer',
            'GET',
            array('email' => $email)
        );

        if ($customer && isset($customer['message']['name'])) {
            return $customer['message']['name'];
        }

        // Create new customer
        $customer_data = array(
            'customer_name' => $order->get_billing_first_name() . ' ' . $order->get_billing_last_name(),
            'email' => $email,
            'phone' => $order->get_billing_phone()
        );

        $result = $this->api_request(
            'erpnext.erpnext_integrations.ecommerce_api.api.create_customer',
            'POST',
            $customer_data
        );

        return $result['message']['name'];
    }

    /**
     * Check stock from ERPNext
     */
    public function check_stock($product_sku) {
        $result = $this->api_request(
            'erpnext.erpnext_integrations.ecommerce_api.api.get_stock_balance',
            'GET',
            array('item_code' => $product_sku)
        );

        return isset($result['message']) ? floatval($result['message']) : 0;
    }
}

// Initialize the plugin
new ERPNext_Integration();
```

---

## Webhooks

To receive real-time updates from ERPNext, you can set up webhooks in ERPNext:

1. Go to **Webhook** in ERPNext
2. Create new Webhook
3. Select DocType (e.g., "Sales Order")
4. Set Webhook URL (your WordPress endpoint)
5. Define conditions and fields to send

### Example Webhook Handler in WordPress

```php
add_action('rest_api_init', function() {
    register_rest_route('erpnext/v1', '/webhook', array(
        'methods' => 'POST',
        'callback' => 'handle_erpnext_webhook',
        'permission_callback' => '__return_true'
    ));
});

function handle_erpnext_webhook(WP_REST_Request $request) {
    $data = $request->get_json_params();

    // Process webhook data
    if (isset($data['doctype']) && $data['doctype'] === 'Sales Order') {
        // Update order status in WooCommerce
        $order_id = get_erpnext_order_mapping($data['name']);
        if ($order_id) {
            $order = wc_get_order($order_id);
            $order->update_status($data['status']);
        }
    }

    return new WP_REST_Response(array('success' => true), 200);
}
```

---

## Error Handling

All API endpoints return standard ERPNext error responses:

### Success Response

```json
{
  "message": { /* response data */ }
}
```

### Error Response

```json
{
  "_server_messages": "[{\"message\": \"Error message here\"}]",
  "exc": "Traceback...",
  "exception": "Error details"
}
```

### Common Error Codes

| HTTP Code | Description |
|-----------|-------------|
| 200 | Success |
| 400 | Bad Request (validation error) |
| 401 | Unauthorized (authentication failed) |
| 403 | Forbidden (insufficient permissions) |
| 404 | Not Found (resource doesn't exist) |
| 500 | Internal Server Error |

---

## Examples

### Complete Order Flow

```python
import requests

# Configuration
API_URL = "https://your-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api"
API_KEY = "your_api_key"
API_SECRET = "your_api_secret"

headers = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Content-Type": "application/json"
}

# 1. Create Customer
customer_data = {
    "customer_name": "John Doe",
    "email": "john@example.com",
    "phone": "+1234567890"
}

customer_response = requests.post(
    f"{API_URL}.create_customer",
    headers=headers,
    json=customer_data
)
customer = customer_response.json()["message"]
print(f"Customer created: {customer['name']}")

# 2. Create Address
address_data = {
    "customer_name": customer["name"],
    "address_line1": "123 Main St",
    "city": "New York",
    "state": "NY",
    "pincode": "10001",
    "country": "United States",
    "address_type": "Shipping",
    "is_shipping": 1
}

address_response = requests.post(
    f"{API_URL}.create_address",
    headers=headers,
    json=address_data
)
address = address_response.json()["message"]
print(f"Address created: {address['name']}")

# 3. Check Stock
items_to_order = [
    {"item_code": "PROD-001", "qty": 2},
    {"item_code": "PROD-002", "qty": 1}
]

stock_check = requests.post(
    f"{API_URL}.check_stock_availability",
    headers=headers,
    json={"items": items_to_order}
)
stock_result = stock_check.json()["message"]

all_available = all(item["is_available"] for item in stock_result)
if not all_available:
    print("Some items are out of stock!")
    exit()

# 4. Create Order
order_data = {
    "customer": customer["name"],
    "items": [
        {"item_code": "PROD-001", "qty": 2},
        {"item_code": "PROD-002", "qty": 1}
    ],
    "order_type": "Shopping Cart",
    "price_list": "Standard Selling",
    "shipping_address": address["name"]
}

order_response = requests.post(
    f"{API_URL}.create_order",
    headers=headers,
    json=order_data
)
order = order_response.json()["message"]
print(f"Order created: {order['name']}, Total: {order['grand_total']}")

# 5. Create Payment
payment_data = {
    "payment_type": "Receive",
    "party": customer["name"],
    "amount": order["grand_total"],
    "payment_method": "Credit Card",
    "reference_no": "TXN-12345",
    "reference_doctype": "Sales Order",
    "reference_name": order["name"]
}

payment_response = requests.post(
    f"{API_URL}.create_payment",
    headers=headers,
    json=payment_data
)
payment = payment_response.json()["message"]
print(f"Payment recorded: {payment['name']}")

# 6. Create Delivery Note
delivery_data = {
    "sales_order": order["name"]
}

delivery_response = requests.post(
    f"{API_URL}.create_delivery_note",
    headers=headers,
    json=delivery_data
)
delivery = delivery_response.json()["message"]
print(f"Delivery note created: {delivery['name']}")

# 7. Update Tracking
tracking_data = {
    "delivery_note": delivery["name"],
    "tracking_number": "1Z999AA10123456784",
    "carrier": "UPS"
}

tracking_response = requests.post(
    f"{API_URL}.update_tracking_info",
    headers=headers,
    json=tracking_data
)
print("Tracking info updated")

# 8. Create Invoice
invoice_data = {
    "sales_order": order["name"]
}

invoice_response = requests.post(
    f"{API_URL}.create_invoice",
    headers=headers,
    json=invoice_data
)
invoice = invoice_response.json()["message"]
print(f"Invoice created: {invoice['name']}")
```

---

## Support

For issues or questions:

1. Check ERPNext documentation: https://docs.erpnext.com
2. ERPNext Forum: https://discuss.erpnext.com
3. GitHub Issues: https://github.com/frappe/erpnext

---

## License

This integration is part of ERPNext and follows the same license (GNU GPL v3).

---

## Changelog

### Version 1.0.0 (2025-10-30)
- Initial release
- Complete REST API for WordPress/WooCommerce integration
- Support for products, orders, customers, inventory, payments, and shipping
