<?php

namespace App\Models;

interface Repository
{
    public function put(string $value): void;

    public function size(): int;
}
