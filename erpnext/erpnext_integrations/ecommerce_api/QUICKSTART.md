# Quick Start Guide - ERPNext E-Commerce Integration

Get started with the ERPNext E-Commerce Integration API in 15 minutes.

## Prerequisites

- ERPNext instance (v14 or higher)
- E-commerce website or application
- Basic knowledge of REST APIs

---

## Step 1: Setup ERPNext (5 minutes)

### 1.1 Create API User

```bash
# Login to ERPNext
# Navigate to: User List > New User
```

Create a user with these details:
- **Email**: wordpress_api@yourcompany.com
- **First Name**: WordPress
- **Last Name**: API User
- **Roles**:
  - Sales User
  - Stock User
  - Accounts User
  - Item Manager

### 1.2 Generate API Keys

1. Open the user you just created
2. Scroll to **API Access** section
3. Click **Generate Keys**
4. **IMPORTANT**: Copy both **API Key** and **API Secret** immediately
5. Save them securely (you won't see the secret again)

Example:
```
API Key: 1a2b3c4d5e6f7g8h
API Secret: 9i8h7g6f5e4d3c2b1a
```

### 1.3 Test API Connection

```bash
# Test ping endpoint (no auth required)
curl https://your-erpnext-site.com/api/method/erpnext.erpnext_integrations.ecommerce_api.api.ping
```

Expected response:
```json
{
  "message": {
    "status": "ok",
    "message": "ERPNext WordPress WooCommerce API is running"
  }
}
```

---

## Step 2: Setup Sample Data in ERPNext (5 minutes)

### 2.1 Create Test Items

```bash
# Via ERPNext UI:
# Stock > Item > New Item
```

Create 2-3 test items:
- **Item Code**: PROD-001
- **Item Name**: Sample Product 1
- **Item Group**: Products
- **Standard Rate**: 99.00
- **Is Stock Item**: ✓ (checked)
- **Opening Stock**: 100 units

### 2.2 Create Price List

```bash
# Selling > Price List > New
```

Create a price list:
- **Price List Name**: Standard Selling
- **Currency**: USD
- **Enabled**: ✓
- **Selling**: ✓

### 2.3 Add Item Prices

```bash
# Stock > Item Price > New
```

Add prices for your items:
- **Item Code**: PROD-001
- **Price List**: Standard Selling
- **Rate**: 99.00

---

## Step 3: Test API Endpoints (5 minutes)

### 3.1 Set Environment Variables

```bash
# Save these for testing
export ERPNEXT_URL="https://your-erpnext-site.com"
export API_KEY="your_api_key"
export API_SECRET="your_api_secret"
```

### 3.2 Test Product API

```bash
# Get all products
curl -X GET \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products?price_list=Standard%20Selling" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}"
```

### 3.3 Test Stock Check

```bash
# Check stock for item
curl -X GET \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_stock_balance?item_code=PROD-001" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}"
```

### 3.4 Create Test Customer

```bash
curl -X POST \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.create_customer" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "Test Customer",
    "email": "test@example.com",
    "phone": "+1234567890"
  }'
```

### 3.5 Create Test Order

```bash
curl -X POST \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.create_order" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}" \
  -H "Content-Type: application/json" \
  -d '{
    "customer": "Test Customer",
    "items": [
      {
        "item_code": "PROD-001",
        "qty": 2
      }
    ],
    "price_list": "Standard Selling"
  }'
```

---

## Step 4: WordPress Integration

### 4.1 Install WordPress Plugin (Option 1 - Manual)

Create a simple WordPress plugin to test:

**File**: `wp-content/plugins/erpnext-integration/erpnext-integration.php`

```php
<?php
/**
 * Plugin Name: ERPNext Integration
 * Description: Simple ERPNext API integration
 * Version: 1.0
 */

// Add settings page
add_action('admin_menu', function() {
    add_options_page(
        'ERPNext Settings',
        'ERPNext',
        'manage_options',
        'erpnext-settings',
        'erpnext_settings_page'
    );
});

function erpnext_settings_page() {
    ?>
    <div class="wrap">
        <h1>ERPNext Integration Settings</h1>
        <form method="post" action="options.php">
            <?php
            settings_fields('erpnext_settings');
            do_settings_sections('erpnext_settings');
            ?>
            <table class="form-table">
                <tr>
                    <th>ERPNext URL</th>
                    <td><input type="text" name="erpnext_url" value="<?php echo get_option('erpnext_url'); ?>" class="regular-text"></td>
                </tr>
                <tr>
                    <th>API Key</th>
                    <td><input type="text" name="erpnext_api_key" value="<?php echo get_option('erpnext_api_key'); ?>" class="regular-text"></td>
                </tr>
                <tr>
                    <th>API Secret</th>
                    <td><input type="password" name="erpnext_api_secret" value="<?php echo get_option('erpnext_api_secret'); ?>" class="regular-text"></td>
                </tr>
            </table>
            <?php submit_button(); ?>
        </form>
    </div>
    <?php
}

// Register settings
add_action('admin_init', function() {
    register_setting('erpnext_settings', 'erpnext_url');
    register_setting('erpnext_settings', 'erpnext_api_key');
    register_setting('erpnext_settings', 'erpnext_api_secret');
});

// Helper function to call ERPNext API
function erpnext_api_call($endpoint, $method = 'GET', $data = []) {
    $url = get_option('erpnext_url') . '/api/method/' . $endpoint;
    $api_key = get_option('erpnext_api_key');
    $api_secret = get_option('erpnext_api_secret');

    $args = [
        'method' => $method,
        'headers' => [
            'Authorization' => 'token ' . $api_key . ':' . $api_secret,
            'Content-Type' => 'application/json'
        ],
        'timeout' => 30
    ];

    if ($method === 'POST') {
        $args['body'] = json_encode($data);
    } elseif ($method === 'GET' && !empty($data)) {
        $url = add_query_arg($data, $url);
    }

    $response = wp_remote_request($url, $args);

    if (is_wp_error($response)) {
        return ['error' => $response->get_error_message()];
    }

    return json_decode(wp_remote_retrieve_body($response), true);
}

// Sync order to ERPNext when created in WooCommerce
add_action('woocommerce_thankyou', function($order_id) {
    $order = wc_get_order($order_id);

    // Prepare items
    $items = [];
    foreach ($order->get_items() as $item) {
        $product = $item->get_product();
        $items[] = [
            'item_code' => $product->get_sku(),
            'qty' => $item->get_quantity(),
            'rate' => $item->get_total() / $item->get_quantity()
        ];
    }

    // Create order in ERPNext
    $result = erpnext_api_call(
        'erpnext.erpnext_integrations.ecommerce_api.api.create_order',
        'POST',
        [
            'customer' => $order->get_billing_email(),
            'items' => $items,
            'price_list' => 'Standard Selling'
        ]
    );

    if (isset($result['message']['name'])) {
        update_post_meta($order_id, '_erpnext_order_id', $result['message']['name']);
        $order->add_order_note('Synced to ERPNext: ' . $result['message']['name']);
    }
});
```

### 4.2 Configure Plugin

1. Activate the plugin in WordPress
2. Go to **Settings > ERPNext**
3. Enter your ERPNext URL, API Key, and API Secret
4. Save settings

### 4.3 Test Integration

1. Create a test product in WooCommerce with SKU matching ERPNext item code
2. Place a test order
3. Check ERPNext for the new Sales Order
4. Verify order details match

---

## Common Issues & Solutions

### Issue: 401 Unauthorized

**Solution**: Check API credentials
```bash
# Verify credentials work
curl -X GET \
  "${ERPNEXT_URL}/api/method/frappe.auth.get_logged_user" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}"
```

### Issue: Item not found

**Solution**: Ensure SKUs match between WooCommerce and ERPNext
```bash
# List all items
curl -X GET \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}"
```

### Issue: Price not showing

**Solution**: Verify item price exists in price list
```bash
# Check item price
curl -X GET \
  "${ERPNEXT_URL}/api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_item_price?item_code=PROD-001&price_list=Standard%20Selling" \
  -H "Authorization: token ${API_KEY}:${API_SECRET}"
```

---

## Next Steps

1. **Sync Products**: Implement product sync from ERPNext to WooCommerce
2. **Real-time Stock**: Set up stock level synchronization
3. **Webhooks**: Configure ERPNext webhooks for real-time updates
4. **Payment Integration**: Connect payment gateways
5. **Order Status**: Sync order status changes bidirectionally

---

## Testing Checklist

- [ ] API credentials work
- [ ] Can fetch products from ERPNext
- [ ] Can check stock levels
- [ ] Can create customer in ERPNext
- [ ] Can create order from WordPress
- [ ] Order appears in ERPNext
- [ ] Stock reduces after order
- [ ] Can create delivery note
- [ ] Can create invoice

---

## Production Checklist

Before going live:

- [ ] Use strong API credentials
- [ ] Enable HTTPS on both sites
- [ ] Set up error logging
- [ ] Configure webhooks for real-time sync
- [ ] Test with production data
- [ ] Set up monitoring
- [ ] Create backup strategy
- [ ] Document custom configurations
- [ ] Train staff on ERPNext
- [ ] Test order fulfillment workflow

---

## Need Help?

- Read the full [README.md](README.md)
- Check [ERPNext Documentation](https://docs.erpnext.com)
- Visit [ERPNext Forum](https://discuss.erpnext.com)
- Review API examples in documentation

---

**Happy Integrating! 🚀**
