<?php
/**
 * Plugin Name: ERPNext WooCommerce Integration
 * Plugin URI: https://github.com/frappe/erpnext
 * Description: Complete integration between WooCommerce and ERPNext ERP system
 * Version: 1.0.0
 * Author: Frappe Technologies
 * Author URI: https://erpnext.com
 * License: GPL v3
 * Requires at least: 5.0
 * Requires PHP: 7.4
 * WC requires at least: 5.0
 * WC tested up to: 8.0
 */

if (!defined('ABSPATH')) {
    exit; // Exit if accessed directly
}

class ERPNext_WooCommerce_Integration {

    private $api_url;
    private $api_key;
    private $api_secret;
    private $price_list;
    private $warehouse;

    public function __construct() {
        // Load settings
        $this->api_url = get_option('erpnext_api_url');
        $this->api_key = get_option('erpnext_api_key');
        $this->api_secret = get_option('erpnext_api_secret');
        $this->price_list = get_option('erpnext_price_list', 'Standard Selling');
        $this->warehouse = get_option('erpnext_warehouse', '');

        // Admin hooks
        add_action('admin_menu', array($this, 'add_admin_menu'));
        add_action('admin_init', array($this, 'register_settings'));

        // WooCommerce hooks
        add_action('woocommerce_thankyou', array($this, 'sync_order_to_erpnext'), 10, 1);
        add_action('woocommerce_order_status_changed', array($this, 'update_order_status'), 10, 3);
        add_filter('woocommerce_product_get_stock_quantity', array($this, 'get_stock_from_erpnext'), 10, 2);
        add_action('woocommerce_payment_complete', array($this, 'create_payment_in_erpnext'), 10, 1);

        // Custom hooks
        add_action('erpnext_sync_all_products', array($this, 'sync_all_products_from_erpnext'));
        add_action('erpnext_sync_stock', array($this, 'sync_stock_from_erpnext'));

        // Scheduled tasks
        if (!wp_next_scheduled('erpnext_sync_stock')) {
            wp_schedule_event(time(), 'hourly', 'erpnext_sync_stock');
        }
    }

    /**
     * Add admin menu
     */
    public function add_admin_menu() {
        add_menu_page(
            'ERPNext Integration',
            'ERPNext',
            'manage_options',
            'erpnext-integration',
            array($this, 'settings_page'),
            'dashicons-admin-settings',
            56
        );

        add_submenu_page(
            'erpnext-integration',
            'ERPNext Settings',
            'Settings',
            'manage_options',
            'erpnext-integration',
            array($this, 'settings_page')
        );

        add_submenu_page(
            'erpnext-integration',
            'Sync Logs',
            'Sync Logs',
            'manage_options',
            'erpnext-sync-logs',
            array($this, 'sync_logs_page')
        );
    }

    /**
     * Register settings
     */
    public function register_settings() {
        register_setting('erpnext_settings', 'erpnext_api_url');
        register_setting('erpnext_settings', 'erpnext_api_key');
        register_setting('erpnext_settings', 'erpnext_api_secret');
        register_setting('erpnext_settings', 'erpnext_price_list');
        register_setting('erpnext_settings', 'erpnext_warehouse');
        register_setting('erpnext_settings', 'erpnext_auto_sync_orders');
        register_setting('erpnext_settings', 'erpnext_auto_sync_stock');
    }

    /**
     * Settings page
     */
    public function settings_page() {
        ?>
        <div class="wrap">
            <h1>ERPNext Integration Settings</h1>

            <?php
            // Test connection
            if (isset($_POST['test_connection'])) {
                $result = $this->test_connection();
                if ($result) {
                    echo '<div class="notice notice-success"><p>Connection successful!</p></div>';
                } else {
                    echo '<div class="notice notice-error"><p>Connection failed. Please check your credentials.</p></div>';
                }
            }

            // Sync products
            if (isset($_POST['sync_products'])) {
                $count = $this->sync_all_products_from_erpnext();
                echo '<div class="notice notice-success"><p>Synced ' . $count . ' products from ERPNext.</p></div>';
            }
            ?>

            <form method="post" action="options.php">
                <?php
                settings_fields('erpnext_settings');
                do_settings_sections('erpnext_settings');
                ?>

                <table class="form-table">
                    <tr>
                        <th scope="row"><label for="erpnext_api_url">ERPNext URL</label></th>
                        <td>
                            <input type="text" id="erpnext_api_url" name="erpnext_api_url"
                                   value="<?php echo esc_attr(get_option('erpnext_api_url')); ?>"
                                   class="regular-text" placeholder="https://your-erpnext-site.com">
                            <p class="description">Your ERPNext site URL (without trailing slash)</p>
                        </td>
                    </tr>

                    <tr>
                        <th scope="row"><label for="erpnext_api_key">API Key</label></th>
                        <td>
                            <input type="text" id="erpnext_api_key" name="erpnext_api_key"
                                   value="<?php echo esc_attr(get_option('erpnext_api_key')); ?>"
                                   class="regular-text">
                        </td>
                    </tr>

                    <tr>
                        <th scope="row"><label for="erpnext_api_secret">API Secret</label></th>
                        <td>
                            <input type="password" id="erpnext_api_secret" name="erpnext_api_secret"
                                   value="<?php echo esc_attr(get_option('erpnext_api_secret')); ?>"
                                   class="regular-text">
                        </td>
                    </tr>

                    <tr>
                        <th scope="row"><label for="erpnext_price_list">Price List</label></th>
                        <td>
                            <input type="text" id="erpnext_price_list" name="erpnext_price_list"
                                   value="<?php echo esc_attr(get_option('erpnext_price_list', 'Standard Selling')); ?>"
                                   class="regular-text">
                            <p class="description">Default: Standard Selling</p>
                        </td>
                    </tr>

                    <tr>
                        <th scope="row"><label for="erpnext_warehouse">Warehouse</label></th>
                        <td>
                            <input type="text" id="erpnext_warehouse" name="erpnext_warehouse"
                                   value="<?php echo esc_attr(get_option('erpnext_warehouse')); ?>"
                                   class="regular-text" placeholder="Leave empty for all warehouses">
                        </td>
                    </tr>

                    <tr>
                        <th scope="row">Auto Sync Options</th>
                        <td>
                            <label>
                                <input type="checkbox" name="erpnext_auto_sync_orders" value="1"
                                       <?php checked(get_option('erpnext_auto_sync_orders'), 1); ?>>
                                Automatically sync orders to ERPNext
                            </label>
                            <br>
                            <label>
                                <input type="checkbox" name="erpnext_auto_sync_stock" value="1"
                                       <?php checked(get_option('erpnext_auto_sync_stock'), 1); ?>>
                                Automatically sync stock levels from ERPNext (hourly)
                            </label>
                        </td>
                    </tr>
                </table>

                <?php submit_button(); ?>
            </form>

            <hr>

            <h2>Actions</h2>
            <form method="post">
                <p>
                    <button type="submit" name="test_connection" class="button button-secondary">
                        Test Connection
                    </button>
                    <button type="submit" name="sync_products" class="button button-primary">
                        Sync All Products from ERPNext
                    </button>
                </p>
            </form>
        </div>
        <?php
    }

    /**
     * Sync logs page
     */
    public function sync_logs_page() {
        global $wpdb;
        $table_name = $wpdb->prefix . 'erpnext_sync_logs';

        // Create table if not exists
        $this->create_logs_table();

        $logs = $wpdb->get_results("SELECT * FROM $table_name ORDER BY created_at DESC LIMIT 100");

        ?>
        <div class="wrap">
            <h1>ERPNext Sync Logs</h1>
            <table class="wp-list-table widefat fixed striped">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Type</th>
                        <th>Status</th>
                        <th>Message</th>
                        <th>Details</th>
                    </tr>
                </thead>
                <tbody>
                    <?php foreach ($logs as $log): ?>
                    <tr>
                        <td><?php echo esc_html($log->created_at); ?></td>
                        <td><?php echo esc_html($log->sync_type); ?></td>
                        <td><?php echo esc_html($log->status); ?></td>
                        <td><?php echo esc_html($log->message); ?></td>
                        <td><code><?php echo esc_html(substr($log->details, 0, 100)); ?>...</code></td>
                    </tr>
                    <?php endforeach; ?>
                </tbody>
            </table>
        </div>
        <?php
    }

    /**
     * Create logs table
     */
    private function create_logs_table() {
        global $wpdb;
        $table_name = $wpdb->prefix . 'erpnext_sync_logs';
        $charset_collate = $wpdb->get_charset_collate();

        $sql = "CREATE TABLE IF NOT EXISTS $table_name (
            id bigint(20) NOT NULL AUTO_INCREMENT,
            sync_type varchar(50) NOT NULL,
            status varchar(20) NOT NULL,
            message text,
            details longtext,
            created_at datetime DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        ) $charset_collate;";

        require_once(ABSPATH . 'wp-admin/includes/upgrade.php');
        dbDelta($sql);
    }

    /**
     * Log sync activity
     */
    private function log_sync($type, $status, $message, $details = '') {
        global $wpdb;
        $table_name = $wpdb->prefix . 'erpnext_sync_logs';

        $wpdb->insert($table_name, array(
            'sync_type' => $type,
            'status' => $status,
            'message' => $message,
            'details' => is_array($details) ? json_encode($details) : $details
        ));
    }

    /**
     * Make API request to ERPNext
     */
    private function api_request($endpoint, $method = 'GET', $data = array()) {
        $url = $this->api_url . '/api/method/' . $endpoint;

        $args = array(
            'method' => $method,
            'headers' => array(
                'Authorization' => 'token ' . $this->api_key . ':' . $this->api_secret,
                'Content-Type' => 'application/json'
            ),
            'timeout' => 30
        );

        if ($method === 'POST' && !empty($data)) {
            $args['body'] = json_encode($data);
        } elseif ($method === 'GET' && !empty($data)) {
            $url = add_query_arg($data, $url);
        }

        $response = wp_remote_request($url, $args);

        if (is_wp_error($response)) {
            $this->log_sync('api_request', 'error', $response->get_error_message(), $endpoint);
            return false;
        }

        $body = wp_remote_retrieve_body($response);
        $result = json_decode($body, true);

        return $result;
    }

    /**
     * Test connection to ERPNext
     */
    private function test_connection() {
        $result = $this->api_request('erpnext.erpnext_integrations.wordpress_woocommerce.api.ping');
        return isset($result['message']['status']) && $result['message']['status'] === 'ok';
    }

    /**
     * Sync order to ERPNext
     */
    public function sync_order_to_erpnext($order_id) {
        if (!get_option('erpnext_auto_sync_orders')) {
            return;
        }

        $order = wc_get_order($order_id);

        // Check if already synced
        $erpnext_order_id = get_post_meta($order_id, '_erpnext_order_id', true);
        if ($erpnext_order_id) {
            return; // Already synced
        }

        // Get or create customer
        $customer_id = $this->get_or_create_customer($order);

        // Prepare items
        $items = array();
        foreach ($order->get_items() as $item) {
            $product = $item->get_product();
            $items[] = array(
                'item_code' => $product->get_sku() ?: $product->get_id(),
                'qty' => $item->get_quantity(),
                'rate' => $item->get_total() / $item->get_quantity()
            );
        }

        // Prepare order data
        $order_data = array(
            'customer' => $customer_id,
            'items' => $items,
            'order_type' => 'Shopping Cart',
            'price_list' => $this->price_list,
            'currency' => $order->get_currency()
        );

        // Create order in ERPNext
        $result = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.create_order',
            'POST',
            $order_data
        );

        if ($result && isset($result['message']['name'])) {
            update_post_meta($order_id, '_erpnext_order_id', $result['message']['name']);
            $order->add_order_note('Order synced to ERPNext: ' . $result['message']['name']);
            $this->log_sync('order_create', 'success', 'Order synced', $result['message']['name']);
        } else {
            $this->log_sync('order_create', 'error', 'Failed to sync order', $result);
        }
    }

    /**
     * Get or create customer in ERPNext
     */
    private function get_or_create_customer($order) {
        $email = $order->get_billing_email();

        // Try to get existing customer
        $customer = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.get_customer',
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
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.create_customer',
            'POST',
            $customer_data
        );

        if ($result && isset($result['message']['name'])) {
            // Create billing address
            $this->create_address($result['message']['name'], $order, 'Billing');

            // Create shipping address if different
            if ($order->has_shipping_address()) {
                $this->create_address($result['message']['name'], $order, 'Shipping');
            }

            return $result['message']['name'];
        }

        return false;
    }

    /**
     * Create address in ERPNext
     */
    private function create_address($customer_id, $order, $type = 'Billing') {
        $prefix = strtolower($type);

        $address_data = array(
            'customer_name' => $customer_id,
            'address_line1' => $order->{"get_{$prefix}_address_1"}(),
            'address_line2' => $order->{"get_{$prefix}_address_2"}(),
            'city' => $order->{"get_{$prefix}_city"}(),
            'state' => $order->{"get_{$prefix}_state"}(),
            'pincode' => $order->{"get_{$prefix}_postcode"}(),
            'country' => $order->{"get_{$prefix}_country"}(),
            'address_type' => $type,
            'is_primary' => $type === 'Billing' ? 1 : 0,
            'is_shipping' => $type === 'Shipping' ? 1 : 0
        );

        $result = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.create_address',
            'POST',
            $address_data
        );

        return $result;
    }

    /**
     * Update order status in ERPNext
     */
    public function update_order_status($order_id, $old_status, $new_status) {
        $erpnext_order_id = get_post_meta($order_id, '_erpnext_order_id', true);

        if (!$erpnext_order_id) {
            return;
        }

        // Map WooCommerce status to ERPNext status
        $status_map = array(
            'completed' => 'Completed',
            'cancelled' => 'Cancelled',
            'refunded' => 'Cancelled'
        );

        if (isset($status_map[$new_status])) {
            $this->api_request(
                'erpnext.erpnext_integrations.wordpress_woocommerce.api.update_order_status',
                'POST',
                array(
                    'order_name' => $erpnext_order_id,
                    'status' => $status_map[$new_status]
                )
            );
        }
    }

    /**
     * Get stock from ERPNext
     */
    public function get_stock_from_erpnext($stock, $product) {
        if (!get_option('erpnext_auto_sync_stock')) {
            return $stock;
        }

        $sku = $product->get_sku();
        if (!$sku) {
            return $stock;
        }

        $result = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.get_stock_balance',
            'GET',
            array(
                'item_code' => $sku,
                'warehouse' => $this->warehouse
            )
        );

        if (isset($result['message'])) {
            return floatval($result['message']);
        }

        return $stock;
    }

    /**
     * Sync all products from ERPNext
     */
    public function sync_all_products_from_erpnext() {
        $result = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.get_products',
            'GET',
            array(
                'price_list' => $this->price_list,
                'page_length' => 100
            )
        );

        if (!$result || !isset($result['message']['items'])) {
            return 0;
        }

        $count = 0;
        foreach ($result['message']['items'] as $item) {
            // Check if product exists
            $product_id = wc_get_product_id_by_sku($item['item_code']);

            if (!$product_id) {
                // Create new product
                $product = new WC_Product_Simple();
                $product->set_sku($item['item_code']);
                $product->set_name($item['item_name']);
                $product->set_description($item['description'] ?? '');
                $product->set_regular_price($item['price_list_rate'] ?? $item['standard_rate']);
                $product->set_manage_stock(true);
                $product->set_stock_quantity($item['stock_qty'] ?? 0);
                $product->save();
                $count++;
            }
        }

        $this->log_sync('product_sync', 'success', "Synced $count products", '');
        return $count;
    }

    /**
     * Sync stock from ERPNext
     */
    public function sync_stock_from_erpnext() {
        if (!get_option('erpnext_auto_sync_stock')) {
            return;
        }

        $products = wc_get_products(array('limit' => -1));

        foreach ($products as $product) {
            $sku = $product->get_sku();
            if (!$sku) {
                continue;
            }

            $stock = $this->get_stock_from_erpnext(0, $product);
            $product->set_stock_quantity($stock);
            $product->save();
        }

        $this->log_sync('stock_sync', 'success', 'Stock synced for all products', '');
    }

    /**
     * Create payment in ERPNext
     */
    public function create_payment_in_erpnext($order_id) {
        $order = wc_get_order($order_id);
        $erpnext_order_id = get_post_meta($order_id, '_erpnext_order_id', true);

        if (!$erpnext_order_id) {
            return;
        }

        $payment_data = array(
            'payment_type' => 'Receive',
            'party' => $this->get_or_create_customer($order),
            'amount' => $order->get_total(),
            'payment_method' => $order->get_payment_method_title(),
            'reference_no' => $order->get_transaction_id(),
            'reference_doctype' => 'Sales Order',
            'reference_name' => $erpnext_order_id
        );

        $result = $this->api_request(
            'erpnext.erpnext_integrations.wordpress_woocommerce.api.create_payment',
            'POST',
            $payment_data
        );

        if ($result && isset($result['message']['name'])) {
            update_post_meta($order_id, '_erpnext_payment_id', $result['message']['name']);
            $order->add_order_note('Payment recorded in ERPNext: ' . $result['message']['name']);
        }
    }
}

// Initialize the plugin
new ERPNext_WooCommerce_Integration();
