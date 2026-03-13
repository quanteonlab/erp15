# ERPNext E-Commerce Integration API - File Index

## Overview

This directory contains a complete e-commerce integration module for ERPNext, providing REST API endpoints and documentation for seamless integration.

---

## Files in This Directory

### 1. **api.py** - Main API Module
**Purpose**: Core REST API implementation with all ecommerce endpoints

**Contains:**
- 40+ whitelisted API endpoints
- Product/Item management APIs
- Inventory and stock management
- Customer CRUD operations
- Order processing and management
- Payment handling
- Coupon/pricing rule validation
- Shipping and delivery tracking
- Invoice generation
- Utility functions

**Key Endpoints:**
- Products: `get_products`, `get_product`, `search_items`, `get_item_variants`
- Inventory: `get_stock_balance`, `check_stock_availability`, `update_stock`
- Customers: `get_customer`, `create_customer`, `create_address`
- Orders: `create_order`, `get_order`, `update_order_status`, `get_customer_orders`
- Payments: `create_payment`, `get_payment_methods`
- Coupons: `validate_coupon`, `apply_coupon_to_order`
- Shipping: `create_delivery_note`, `update_tracking_info`
- Invoices: `create_invoice`, `get_invoice`
- Utilities: `get_item_groups`, `get_price_lists`, `get_warehouses`, `ping`

---

### 2. **utils.py** - Utility Functions
**Purpose**: Helper functions for data transformation and common operations

**Contains:**
- `validate_wordpress_request()` - Request validation
- `format_item_for_wordpress()` - Convert ERPNext item to WooCommerce format
- `format_order_for_erp()` - Convert WooCommerce order to ERPNext format
- `sync_stock_to_wordpress()` - Stock synchronization helper
- `create_webhook_log()` - Webhook activity logging
- `get_or_create_customer_from_wordpress()` - Customer sync helper
- `map_wordpress_payment_status_to_erp()` - Status mapping
- `calculate_shipping_cost()` - Shipping calculation

---

### 3. **README.md** - Complete Documentation
**Purpose**: Comprehensive API documentation and integration guide

**Sections:**
- Overview and features
- Installation instructions
- Authentication setup
- Complete API endpoint documentation
- Request/response examples
- Integration guide with code samples
- Webhook setup
- Error handling
- WordPress plugin development guide
- Production checklist

**Length**: ~1,000 lines of detailed documentation

---

### 4. **QUICKSTART.md** - Quick Start Guide
**Purpose**: Get started in 15 minutes

**Sections:**
- Step-by-step ERPNext setup (5 min)
- Sample data creation (5 min)
- API endpoint testing (5 min)
- WordPress integration setup
- Common issues and solutions
- Testing checklist
- Production checklist

**Perfect for**: First-time users who want to test the integration quickly

---

### 5. **example_wordpress_plugin.php** - Complete WordPress Plugin
**Purpose**: Production-ready WordPress plugin for WooCommerce integration

**Features:**
- Settings page for API configuration
- Automatic order synchronization to ERPNext
- Real-time stock level sync
- Customer creation and address management
- Payment recording
- Order status synchronization
- Sync logs and debugging
- Scheduled tasks for stock updates
- Admin interface in WordPress

**Code**: ~600 lines of fully functional PHP

---

### 6. **ERPNext_WooCommerce_API.postman_collection.json** - Postman Collection
**Purpose**: Ready-to-import API testing collection

**Contains:**
- 40+ pre-configured API requests
- Organized by category (Products, Inventory, Customers, Orders, etc.)
- Environment variables setup
- Authentication configured
- Sample request bodies
- Easy testing workflow

**How to use:**
1. Import into Postman
2. Set environment variables (api_url, api_key, api_secret)
3. Start testing endpoints

---

### 7. **__init__.py** - Python Module Initializer
**Purpose**: Module initialization file for Python package

---

## Quick Reference

### API Base URL Format
```
https://your-erpnext-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.<endpoint_name>
```

### Authentication
```bash
Authorization: token <api_key>:<api_secret>
```

### Example API Call
```bash
curl -X GET \
  'https://your-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products?price_list=Standard%20Selling' \
  -H 'Authorization: token YOUR_KEY:YOUR_SECRET'
```

---

## Integration Workflow

### Typical WordPress → ERPNext Flow

1. **Customer places order on WordPress/WooCommerce**
   ↓
2. **WordPress plugin creates/updates customer in ERPNext**
   `POST /api.create_customer`
   ↓
3. **WordPress plugin creates order in ERPNext**
   `POST /api.create_order`
   ↓
4. **Payment is processed**
   `POST /api.create_payment`
   ↓
5. **ERPNext creates delivery note**
   `POST /api.create_delivery_note`
   ↓
6. **Tracking info synced back to WordPress**
   `POST /api.update_tracking_info`
   ↓
7. **Invoice generated in ERPNext**
   `POST /api.create_invoice`

---

## Key Features

### ✅ Product Management
- Sync products from ERPNext to WordPress
- Support for variants and attributes
- Dynamic pricing with price lists
- Product images and descriptions

### ✅ Real-time Inventory
- Live stock level checking
- Automatic stock reservation
- Multi-warehouse support
- Batch stock updates

### ✅ Order Processing
- Automatic order creation
- Order status synchronization
- Multi-step fulfillment workflow
- Order history and tracking

### ✅ Customer Management
- Auto-create customers from WooCommerce
- Multi-address support
- Customer groups and territories
- Contact information sync

### ✅ Payment Integration
- Multiple payment methods
- Payment recording and reconciliation
- Transaction tracking
- Payment terms support

### ✅ Pricing & Discounts
- Multiple price lists
- Customer-specific pricing
- Coupon code validation
- Dynamic pricing rules

### ✅ Shipping & Delivery
- Delivery note creation
- Shipping carrier integration
- Tracking number updates
- Multi-stop delivery routes

### ✅ Invoicing
- Auto-generate invoices from orders
- Tax calculation
- Payment status tracking
- Invoice history

---

## Getting Started

### For Developers
1. Read **README.md** for complete API documentation
2. Import **Postman collection** for API testing
3. Review **example_wordpress_plugin.php** for integration patterns
4. Use **utils.py** functions for data transformation

### For Quick Testing
1. Follow **QUICKSTART.md** for 15-minute setup
2. Use **Postman collection** to test individual endpoints
3. Check **README.md** examples section for code samples

### For Production Deployment
1. Complete all setup steps in **README.md**
2. Customize **example_wordpress_plugin.php** for your needs
3. Follow production checklist in **QUICKSTART.md**
4. Set up webhooks for real-time sync
5. Configure error logging and monitoring

---

## API Endpoint Categories

### 📦 Products (8 endpoints)
- List products with pagination
- Get product details
- Search products
- Get variants and attributes
- Get pricing information

### 📊 Inventory (3 endpoints)
- Check stock levels
- Batch stock availability
- Update stock quantities

### 👥 Customers (4 endpoints)
- Get customer details
- Create/update customers
- Manage addresses
- Sync contact information

### 🛒 Orders (4 endpoints)
- Create shopping cart orders
- Get order details
- Update order status
- Get customer order history

### 💳 Payments (2 endpoints)
- Record payments
- Get payment methods

### 🎟️ Coupons (2 endpoints)
- Validate coupon codes
- Apply coupons to orders

### 🚚 Shipping (2 endpoints)
- Create delivery notes
- Update tracking information

### 📄 Invoices (2 endpoints)
- Generate invoices
- Get invoice details

### 🔧 Utilities (6 endpoints)
- Get item groups
- Get price lists
- Get warehouses
- Get companies
- Get tax rates
- Health check (ping)

---

## Technology Stack

### Backend (ERPNext)
- **Language**: Python 3
- **Framework**: Frappe Framework
- **Database**: MariaDB/PostgreSQL
- **API**: REST/JSON-RPC

### Frontend (WordPress)
- **Language**: PHP 7.4+
- **Platform**: WordPress 5.0+
- **Plugin**: WooCommerce 5.0+
- **API Client**: wp_remote_request

---

## Support & Resources

### Documentation
- **README.md** - Full API documentation
- **QUICKSTART.md** - Quick setup guide
- **Example Plugin** - Working code samples

### Testing
- **Postman Collection** - API testing toolkit
- **Example Requests** - Sample API calls in documentation

### ERPNext Resources
- Official Docs: https://docs.erpnext.com
- Forum: https://discuss.erpnext.com
- GitHub: https://github.com/frappe/erpnext

### WordPress/WooCommerce Resources
- WooCommerce Docs: https://woocommerce.com/documentation/
- WordPress API: https://developer.wordpress.org/rest-api/
- WooCommerce API: https://woocommerce.github.io/woocommerce-rest-api-docs/

---

## Version History

### v1.0.0 (2025-10-30)
- Initial release
- 40+ API endpoints
- Complete documentation
- WordPress plugin example
- Postman collection
- Quick start guide

---

## License

This integration is part of ERPNext and follows the GNU General Public License v3.0.

---

## Contributors

Developed as part of the ERPNext ecosystem by Frappe Technologies.

---

## Next Steps

1. **Read** the README.md for complete documentation
2. **Try** the QUICKSTART.md guide to set up in 15 minutes
3. **Test** using the Postman collection
4. **Implement** using the example WordPress plugin
5. **Deploy** to production following best practices

---

**Happy Integrating! 🚀**
