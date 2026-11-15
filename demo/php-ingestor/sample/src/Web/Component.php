<?php

namespace App\Web;

abstract class Component
{
    abstract public function render(): string;
}
