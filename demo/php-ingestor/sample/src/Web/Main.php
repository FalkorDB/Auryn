<?php

namespace App\Web;

use App\Models\Logger;
use App\Models\Store;

class Main
{
    public function make(Store $store): Widget
    {
        return new Widget($store, 'main');
    }

    public function run(): void
    {
        $store = new Store(new Logger());
        $widget = $this->make($store);
        $widget->save();
    }
}
